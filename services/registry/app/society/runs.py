"""Run lifecycle: dispatch events to agents, atomic claim with lease,
retry with backoff, dead-run handling and circuit breaker.

Concurrency model
-----------------
* ``dispatch_pending_events`` locks a batch of PENDING events with
  ``FOR UPDATE SKIP LOCKED`` so N workers dispatch disjoint sets.
* ``claim_next_run`` is a single ``UPDATE ... FROM (SELECT ... FOR UPDATE
  SKIP LOCKED) RETURNING`` statement: exactly one worker wins a run. A run
  whose lease expired is claimable again (crash recovery); the ``attempt``
  counter increments on every claim so a poisoned run cannot spin forever.
* ``extend_lease`` is called around long operations (QA test runs) so a
  healthy worker is never pre-empted mid-flight.

Loop-storm guards applied at dispatch (cheap, before any run exists):
  - event TTL, max causation depth, max runs per correlation,
  - an agent is never woken by its own event unless explicitly targeted,
  - UNIQUE(agent_id, event_id) makes double-dispatch impossible.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Set

from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..models import (
    Agent,
    AgentCapabilityGrant,
    AgentRun,
    AgentRunStatus,
    AgentStatus,
    SocietyEvent,
    SocietyEventStatus,
)
from .config import SocietySettings
from .events import EventType, emit_event, expire_stale_events, utcnow

logger = logging.getLogger(__name__)

TERMINAL_RUN_STATUSES = (
    AgentRunStatus.COMPLETED,
    AgentRunStatus.FAILED,
    AgentRunStatus.DEAD,
    AgentRunStatus.SKIPPED,
)


@dataclass
class DispatchStats:
    events_seen: int = 0
    events_dispatched: int = 0
    events_ignored: int = 0
    events_expired: int = 0
    runs_created: int = 0
    duplicates_prevented: int = 0
    loop_breaks: int = 0
    notes: List[str] = field(default_factory=list)


def _agents_for_roles(db: Session, roles: Iterable[str]) -> List[tuple[Agent, AgentCapabilityGrant]]:
    roles = list(roles)
    if not roles:
        return []
    rows = (
        db.query(Agent, AgentCapabilityGrant)
        .join(AgentCapabilityGrant, AgentCapabilityGrant.agent_id == Agent.id)
        .filter(AgentCapabilityGrant.role.in_(roles), AgentCapabilityGrant.enabled.is_(True))
        .filter(Agent.status.in_([AgentStatus.ACTIVE, AgentStatus.UNVERIFIED]))
        .order_by(Agent.name)
        .all()
    )
    return rows


def _targeted_agent(db: Session, event: SocietyEvent) -> Optional[tuple[Agent, AgentCapabilityGrant]]:
    target_id = None
    payload = event.payload or {}
    raw = payload.get("target_agent_id")
    if raw:
        try:
            target_id = uuid.UUID(str(raw))
        except ValueError:
            target_id = None
    if target_id is None and event.subject_type == "agent" and event.subject_id is not None:
        target_id = event.subject_id
    if target_id is None:
        return None
    row = (
        db.query(Agent, AgentCapabilityGrant)
        .join(AgentCapabilityGrant, AgentCapabilityGrant.agent_id == Agent.id)
        .filter(Agent.id == target_id, AgentCapabilityGrant.enabled.is_(True))
        .first()
    )
    return row


def runs_in_correlation(db: Session, correlation_id: uuid.UUID) -> int:
    return int(db.query(func.count(AgentRun.id)).filter(AgentRun.correlation_id == correlation_id).scalar() or 0)


def dispatch_pending_events(
    db: Session,
    *,
    settings: SocietySettings,
    routing: Dict[str, List[str]],
    now: Optional[datetime] = None,
) -> DispatchStats:
    """Route PENDING events to agent runs. Commits."""
    now = now or utcnow()
    stats = DispatchStats()

    expired = expire_stale_events(db, ttl_seconds=settings.event_ttl_seconds, now=now)
    stats.events_expired = expired

    events = (
        db.query(SocietyEvent)
        .filter(SocietyEvent.status == SocietyEventStatus.PENDING)
        .order_by(SocietyEvent.created_at)
        .limit(settings.dispatch_batch_size)
        .with_for_update(skip_locked=True)
        .all()
    )
    for event in events:
        stats.events_seen += 1
        note = None
        if int(event.causation_depth or 0) > settings.max_causation_depth:
            note = f"loop breaker: causation depth {event.causation_depth} > {settings.max_causation_depth}"
        elif runs_in_correlation(db, event.correlation_id) >= settings.max_runs_per_correlation:
            note = f"loop breaker: correlation {event.correlation_id} reached {settings.max_runs_per_correlation} runs"
        if note:
            event.status = SocietyEventStatus.IGNORED
            event.dispatch_note = note
            event.dispatched_at = now
            event.processed_at = now
            stats.events_ignored += 1
            stats.loop_breaks += 1
            # One breaker event per correlation, never chained (no causation, depth 0),
            # so it cannot itself trip the breaker.
            emit_event(
                db,
                event_type=EventType.LOOP_BREAKER_TRIPPED,
                payload={"correlation_id": str(event.correlation_id), "note": note, "event_id": str(event.id)},
                correlation_id=event.correlation_id,
                idempotency_key=f"loop-breaker:{event.correlation_id}",
                notify=False,
            )
            continue

        selected: Dict[uuid.UUID, tuple[Agent, AgentCapabilityGrant]] = {}
        targeted = _targeted_agent(db, event)
        if targeted is not None:
            selected[targeted[0].id] = targeted
        for agent, grant in _agents_for_roles(db, routing.get(event.event_type, [])):
            if event.actor_type == "agent" and event.actor_id == agent.id and targeted is None:
                continue  # never wake an agent on its own untargeted event
            selected.setdefault(agent.id, (agent, grant))

        created = 0
        for agent, grant in selected.values():
            stmt = (
                pg_insert(AgentRun.__table__)
                .values(
                    id=uuid.uuid4(),
                    agent_id=agent.id,
                    event_id=event.id,
                    role=grant.role,
                    status=AgentRunStatus.QUEUED.value,
                    attempt=0,
                    max_attempts=settings.run_max_attempts,
                    correlation_id=event.correlation_id,
                    trace_id=event.trace_id,
                    span_id=uuid.uuid4(),
                    context_summary={},
                    intents_count=0,
                    cost_usd=0,
                )
                .on_conflict_do_nothing(constraint="uq_agent_runs_agent_event")
            )
            res = db.execute(stmt)
            if res.rowcount:
                created += 1
            else:
                stats.duplicates_prevented += 1
        stats.runs_created += created
        event.dispatched_at = now
        if created == 0 and not selected:
            event.status = SocietyEventStatus.IGNORED
            event.dispatch_note = "no subscriber"
            event.processed_at = now
            stats.events_ignored += 1
        else:
            event.status = SocietyEventStatus.DISPATCHED
            event.dispatch_note = f"{created} run(s) created for {[a.name for a, _ in selected.values()]}"
            stats.events_dispatched += 1
    db.commit()
    return stats


_CLAIM_SQL = text(
    """
    WITH candidate AS (
        SELECT id FROM agent_runs
        WHERE (status = 'queued' AND (not_before IS NULL OR not_before <= :now))
           OR (status IN ('claimed', 'running') AND lease_expires_at IS NOT NULL AND lease_expires_at < :now)
        ORDER BY created_at
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    UPDATE agent_runs r
       SET status = 'claimed',
           worker_id = :worker_id,
           lease_expires_at = :lease_until,
           attempt = r.attempt + 1,
           updated_at = :now
      FROM candidate
     WHERE r.id = candidate.id
 RETURNING r.id
    """
)


def claim_next_run(db: Session, *, worker_id: str, lease_seconds: int, now: Optional[datetime] = None) -> Optional[AgentRun]:
    """Atomically claim one claimable run. Commits. Returns None if nothing is claimable."""
    now = now or utcnow()
    row = db.execute(
        _CLAIM_SQL,
        {"now": now, "worker_id": worker_id, "lease_until": now + timedelta(seconds=lease_seconds)},
    ).fetchone()
    db.commit()
    if row is None:
        return None
    run = db.query(AgentRun).filter(AgentRun.id == row[0]).first()
    if run is not None:
        db.refresh(run)
    return run


def extend_lease(db: Session, run: AgentRun, *, lease_seconds: int, now: Optional[datetime] = None) -> None:
    now = now or utcnow()
    run.lease_expires_at = now + timedelta(seconds=lease_seconds)
    run.updated_at = now
    db.commit()


def mark_running(db: Session, run: AgentRun, *, now: Optional[datetime] = None) -> None:
    now = now or utcnow()
    run.status = AgentRunStatus.RUNNING
    run.started_at = run.started_at or now
    run.updated_at = now
    db.commit()


def _finalize_event_if_done(db: Session, event_id: uuid.UUID, now: datetime) -> None:
    db.flush()  # the caller just changed this run's status in-session; make the count see it
    open_runs = (
        db.query(func.count(AgentRun.id))
        .filter(AgentRun.event_id == event_id, AgentRun.status.notin_([s.value for s in TERMINAL_RUN_STATUSES]))
        .scalar()
    )
    if not open_runs:
        ev = db.query(SocietyEvent).filter(SocietyEvent.id == event_id).first()
        if ev is not None and ev.status == SocietyEventStatus.DISPATCHED:
            ev.status = SocietyEventStatus.PROCESSED
            ev.processed_at = now


def complete_run(db: Session, run: AgentRun, *, now: Optional[datetime] = None) -> None:
    now = now or utcnow()
    run.status = AgentRunStatus.COMPLETED
    run.completed_at = now
    run.lease_expires_at = None
    run.updated_at = now
    grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == run.agent_id).first()
    if grant is not None and grant.consecutive_failures:
        grant.consecutive_failures = 0
    _finalize_event_if_done(db, run.event_id, now)
    db.commit()


def skip_run(db: Session, run: AgentRun, reason: str, *, now: Optional[datetime] = None) -> None:
    now = now or utcnow()
    run.status = AgentRunStatus.SKIPPED
    run.error = reason[:2000]
    run.completed_at = now
    run.lease_expires_at = None
    run.updated_at = now
    _finalize_event_if_done(db, run.event_id, now)
    db.commit()


def backoff_seconds(settings: SocietySettings, attempt: int) -> int:
    return int(settings.retry_backoff_base_seconds * (2 ** max(0, attempt - 1)))


def fail_run(
    db: Session,
    run: AgentRun,
    error: str,
    *,
    settings: SocietySettings,
    retryable: bool = True,
    now: Optional[datetime] = None,
) -> AgentRunStatus:
    """Record a failed attempt. Re-queues with exponential backoff while
    attempts remain; otherwise marks DEAD, bumps the agent's failure
    counter and trips the circuit breaker when the threshold is reached.
    Commits. Returns the resulting status."""
    now = now or utcnow()
    run.error = (error or "unknown error")[:4000]
    run.updated_at = now
    if retryable and run.attempt < run.max_attempts:
        run.status = AgentRunStatus.QUEUED
        run.worker_id = None
        run.lease_expires_at = None
        run.not_before = now + timedelta(seconds=backoff_seconds(settings, run.attempt))
        db.commit()
        return AgentRunStatus.QUEUED

    run.status = AgentRunStatus.DEAD
    run.completed_at = now
    run.lease_expires_at = None
    grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == run.agent_id).first()
    if grant is not None:
        grant.consecutive_failures = int(grant.consecutive_failures or 0) + 1
        if grant.consecutive_failures >= settings.circuit_breaker_failures:
            grant.paused_until = now + timedelta(seconds=settings.circuit_breaker_pause_seconds)
            emit_event(
                db,
                event_type=EventType.LOOP_BREAKER_TRIPPED,
                payload={
                    "agent_id": str(run.agent_id),
                    "reason": f"{grant.consecutive_failures} consecutive dead runs",
                    "paused_until": grant.paused_until.isoformat(),
                },
                correlation_id=run.correlation_id,
                idempotency_key=f"circuit-breaker:{run.agent_id}:{grant.paused_until.isoformat()}",
                notify=False,
            )
    emit_event(
        db,
        event_type=EventType.RUN_DEAD,
        payload={"run_id": str(run.id), "agent_id": str(run.agent_id), "error": run.error[:500]},
        correlation_id=run.correlation_id,
        idempotency_key=f"run-dead:{run.id}",
        notify=False,
    )
    _finalize_event_if_done(db, run.event_id, now)
    db.commit()
    return AgentRunStatus.DEAD


def reap_expired_leases(db: Session, *, now: Optional[datetime] = None) -> int:
    """Diagnostics only — claim_next_run already treats expired leases as
    claimable. Returns how many runs currently have an expired lease."""
    now = now or utcnow()
    return int(
        db.query(func.count(AgentRun.id))
        .filter(
            AgentRun.status.in_([AgentRunStatus.CLAIMED, AgentRunStatus.RUNNING]),
            AgentRun.lease_expires_at < now,
        )
        .scalar()
        or 0
    )


def pending_work_exists(db: Session, *, now: Optional[datetime] = None) -> bool:
    now = now or utcnow()
    if db.query(SocietyEvent.id).filter(SocietyEvent.status == SocietyEventStatus.PENDING).first():
        return True
    return (
        db.query(AgentRun.id)
        .filter(
            (AgentRun.status == AgentRunStatus.QUEUED) & ((AgentRun.not_before.is_(None)) | (AgentRun.not_before <= now))
        )
        .first()
        is not None
    )


def next_wake_deadline(db: Session, *, now: Optional[datetime] = None) -> Optional[datetime]:
    """Earliest not_before / lease expiry the worker should wake for."""
    now = now or utcnow()
    nb = db.query(func.min(AgentRun.not_before)).filter(AgentRun.status == AgentRunStatus.QUEUED, AgentRun.not_before > now).scalar()
    le = (
        db.query(func.min(AgentRun.lease_expires_at))
        .filter(AgentRun.status.in_([AgentRunStatus.CLAIMED, AgentRunStatus.RUNNING]), AgentRun.lease_expires_at > now)
        .scalar()
    )
    candidates = [d for d in (nb, le) if d is not None]
    return min(candidates) if candidates else None


def terminal(status) -> bool:
    return status in TERMINAL_RUN_STATUSES or str(status) in {s.value for s in TERMINAL_RUN_STATUSES}


def agent_ids_with_grants(db: Session) -> Set[uuid.UUID]:
    return {r[0] for r in db.query(AgentCapabilityGrant.agent_id).filter(AgentCapabilityGrant.enabled.is_(True)).all()}
