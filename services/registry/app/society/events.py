"""Durable society events (outbox) + wake signalling.

``emit_event`` inserts a ``society_events`` row inside the caller's
transaction and issues a transactional ``pg_notify`` on the same
connection. Postgres delivers the NOTIFY only when the transaction commits,
so a worker can never be woken for an event that was rolled back, and an
event can never exist without the notification having been attempted. If
no worker is listening the row simply waits: the poll fallback in
``worker.py`` picks it up. Redis is *not* involved in durability.

Idempotency: pass ``idempotency_key`` for anything that may be emitted
twice (retried run, replayed webhook). A duplicate returns the existing
row and inserts nothing.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..models import SocietyEvent, SocietyEventStatus

logger = logging.getLogger(__name__)

WAKE_CHANNEL = "society_wake"


class EventType:
    """Well-known event types. Free-form types are allowed (external
    integrations) but only these have default subscribers in roles.py."""

    # world / platform signals
    PLATFORM_METRIC_ANOMALY = "platform.metric.anomaly"
    TASK_FAILED = "task.failed"
    TASK_TIMEOUT = "task.timeout"
    TASK_COMPLETED = "task.completed"
    QA_FAILED = "qa.failed"
    AGENT_INACTIVE = "agent.inactive"
    SOCIETY_HEARTBEAT = "society.heartbeat"
    # society-produced
    AGENT_MESSAGE_RECEIVED = "agent.message.received"
    MEMORY_WRITTEN = "memory.written"
    GOAL_CREATED = "goal.created"
    GOAL_UPDATED = "goal.updated"
    PROPOSAL_CREATED = "proposal.created"
    PROPOSAL_APPROVED = "proposal.approved"
    PROPOSAL_REJECTED = "proposal.rejected"
    OFFER_CREATED = "offer.created"
    OFFER_ACCEPTED = "offer.accepted"
    TASK_CREATED = "task.created"
    CODE_CHANGE_REQUESTED = "code_change.requested"
    CODE_CANDIDATE_BUILT = "code_candidate.built"
    CODE_CANDIDATE_QA_PASSED = "code_candidate.qa_passed"
    CODE_CANDIDATE_QA_FAILED = "code_candidate.qa_failed"
    CODE_CANDIDATE_SECURITY_REVIEW = "code_candidate.security_review"
    CODE_CANDIDATE_READY = "code_candidate.ready"
    CODE_CANDIDATE_REJECTED = "code_candidate.rejected"
    STAGING_DEPLOY_REQUESTED = "staging_deploy.requested"
    INTENT_DENIED = "intent.denied"
    INTENT_APPROVAL_REQUIRED = "intent.approval_required"
    INTENT_APPROVED = "intent.approved"
    INTENT_REJECTED = "intent.rejected"
    INTENT_RESUMED = "intent.resumed"
    INTENT_EXECUTED = "intent.executed"
    # world events accepted from outside (see config.ingress_event_allowlist)
    PLATFORM_HEALTH_DEGRADED = "platform.health.degraded"
    USER_FEEDBACK_RECEIVED = "user.feedback.received"
    STAGING_CANARY_SIGNAL = "staging.canary.signal"
    RUN_DEAD = "run.dead"
    LOOP_BREAKER_TRIPPED = "loop_breaker.tripped"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def emit_event(
    db: Session,
    *,
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
    actor_type: str = "system",
    actor_id: Optional[uuid.UUID] = None,
    subject_type: Optional[str] = None,
    subject_id: Optional[uuid.UUID] = None,
    correlation_id: Optional[uuid.UUID] = None,
    causation: Optional[SocietyEvent] = None,
    idempotency_key: Optional[str] = None,
    source_run_id: Optional[uuid.UUID] = None,
    trace_id: Optional[uuid.UUID] = None,
    notify: bool = True,
) -> SocietyEvent:
    """Append an event. Returns the new row, or the existing row when
    ``idempotency_key`` already exists. Does NOT commit."""
    if not event_type or len(event_type) > 128:
        raise ValueError("event_type must be 1..128 chars")
    if idempotency_key is not None and len(idempotency_key) > 160:
        raise ValueError("idempotency_key too long")

    if causation is not None:
        correlation_id = correlation_id or causation.correlation_id
        depth = int(causation.causation_depth or 0) + 1
        causation_id = causation.id
        trace_id = trace_id or causation.trace_id
    else:
        depth = 0
        causation_id = None
    correlation_id = correlation_id or uuid.uuid4()
    trace_id = trace_id or correlation_id

    event_id = uuid.uuid4()
    values = dict(
        id=event_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        subject_type=subject_type,
        subject_id=subject_id,
        payload=payload or {},
        correlation_id=correlation_id,
        causation_id=causation_id,
        causation_depth=depth,
        idempotency_key=idempotency_key,
        status=SocietyEventStatus.PENDING.value,
        trace_id=trace_id,
        source_run_id=source_run_id,
    )
    stmt = pg_insert(SocietyEvent.__table__).values(**values)
    if idempotency_key is not None:
        stmt = stmt.on_conflict_do_nothing(index_elements=["idempotency_key"])
    result = db.execute(stmt)
    if result.rowcount == 0 and idempotency_key is not None:
        existing = db.query(SocietyEvent).filter(SocietyEvent.idempotency_key == idempotency_key).first()
        if existing is not None:
            logger.info("society event dedup: %s key=%s -> existing %s", event_type, idempotency_key, existing.id)
            existing.deduplicated = True  # transient marker for callers (not a column)
            return existing
    if notify:
        db.execute(text("SELECT pg_notify(:ch, :payload)"), {"ch": WAKE_CHANNEL, "payload": str(event_id)})
    row = db.query(SocietyEvent).filter(SocietyEvent.id == event_id).first()
    row.deduplicated = False
    return row


def expire_stale_events(db: Session, *, ttl_seconds: int, now: Optional[datetime] = None) -> int:
    """Mark PENDING events older than the TTL as EXPIRED (they will not be
    dispatched). Returns the number expired. Caller commits."""
    now = now or utcnow()
    cutoff = now.timestamp() - ttl_seconds
    cutoff_dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
    n = (
        db.query(SocietyEvent)
        .filter(SocietyEvent.status == SocietyEventStatus.PENDING, SocietyEvent.created_at < cutoff_dt)
        .update(
            {
                SocietyEvent.status: SocietyEventStatus.EXPIRED,
                SocietyEvent.dispatch_note: "expired: TTL elapsed before dispatch",
                SocietyEvent.processed_at: now,
            },
            synchronize_session=False,
        )
    )
    return int(n or 0)
