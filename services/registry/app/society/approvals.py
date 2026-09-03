"""Durable human approval + resume for ``awaiting_approval`` intents.

Lifecycle (all transitions under ``SELECT ... FOR UPDATE`` on the intent):

    awaiting_approval ──approve──▶ approved ──worker resume──▶ executed | failed | denied
    awaiting_approval ──reject───▶ rejected  (terminal; never executes)

Properties (tested in tests/society/test_approvals.py):

* exactly one ``intent_approvals`` row per intent (UNIQUE); the first
  decision wins, the same decision repeated is idempotent (200), a
  conflicting decision after the first is refused (409);
* approve-after-reject / reject-after-approve / reject-once-executing are
  impossible: the row lock serialises operators and the state check refuses
  anything that is not ``awaiting_approval``;
* resume never calls the model: the persisted, typed intent is re-validated
  and re-adjudicated by ``policy.evaluate_intent(..., approval_granted=True)``
  against the *current* grant, flags and payload. Approval satisfies only
  the ``approval_required`` condition; forbidden HIGH types stay denied;
* resume is claimed with a lease (``resume_worker_id`` /
  ``resume_lease_expires_at``) via ``FOR UPDATE SKIP LOCKED`` so two workers
  cannot execute the same approved intent, and a crashed worker's claim
  expires and is retried a bounded number of times;
* every step emits a causation-linked event (``intent.approved``,
  ``intent.rejected``, ``intent.resumed``, ``intent.executed``,
  ``intent.denied``) with intent-derived idempotency keys.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import (
    Agent,
    AgentCapabilityGrant,
    AgentIntent,
    AgentRun,
    ApprovalDecision,
    IntentApproval,
    IntentExecutionStatus,
    PolicyDecision,
    SocietyEvent,
    SpanStatus,
    User,
)
from .config import SocietySettings
from .events import EventType, emit_event, utcnow
from .executor import ExecContext, ExecutionError, execute
from .intents import FORBIDDEN_INTENT_TYPES, PAYLOAD_MODELS, IntentType, ValidatedIntent
from .policy import evaluate_intent

logger = logging.getLogger(__name__)


class ApprovalConflict(Exception):
    """The intent is not (or no longer) awaiting approval, or a conflicting
    decision already exists."""


class ApprovalNotFound(Exception):
    pass


@dataclass
class DecisionResult:
    approval: IntentApproval
    intent: AgentIntent
    already_decided: bool = False


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _emit(db: Session, *, intent: AgentIntent, run: AgentRun, event: Optional[SocietyEvent], event_type: str, payload: dict, actor_type: str = "user", actor_id=None, notify: bool = False) -> SocietyEvent:
    return emit_event(
        db,
        event_type=event_type,
        payload=payload,
        actor_type=actor_type,
        actor_id=actor_id,
        subject_type="intent",
        subject_id=intent.id,
        correlation_id=run.correlation_id,
        causation=event,
        idempotency_key=f"approval:{intent.idempotency_key}:{event_type}"[:160],
        source_run_id=run.id,
        trace_id=run.trace_id,
        notify=notify,
    )


def decide(db: Session, *, intent_id: uuid.UUID, user: User, decision: str, reason: Optional[str] = None) -> DecisionResult:
    """Record a human decision. Commits. Raises ApprovalNotFound / ApprovalConflict."""
    decision_enum = ApprovalDecision(decision)
    intent = db.query(AgentIntent).filter(AgentIntent.id == intent_id).with_for_update().first()
    if intent is None:
        raise ApprovalNotFound(str(intent_id))
    status = _ev(intent.execution_status)
    existing = db.query(IntentApproval).filter(IntentApproval.intent_id == intent.id).first()
    if existing is not None:
        if _ev(existing.decision) == decision_enum.value:
            db.rollback()  # release the lock; nothing changed
            return DecisionResult(approval=existing, intent=intent, already_decided=True)
        db.rollback()
        raise ApprovalConflict(f"intent already {_ev(existing.decision)} by another operator")
    if status != IntentExecutionStatus.AWAITING_APPROVAL.value:
        db.rollback()
        raise ApprovalConflict(f"intent is {status}, not awaiting_approval")
    try:
        IntentType(intent.intent_type)
    except ValueError:
        db.rollback()
        raise ApprovalConflict("unknown intent type cannot be approved")
    if decision_enum == ApprovalDecision.APPROVED and IntentType(intent.intent_type) in FORBIDDEN_INTENT_TYPES:
        db.rollback()
        raise ApprovalConflict(f"{intent.intent_type} is a forbidden HIGH-risk type and can never be approved")

    run = db.query(AgentRun).filter(AgentRun.id == intent.run_id).first()
    event = db.query(SocietyEvent).filter(SocietyEvent.id == run.event_id).first() if run else None
    approval = IntentApproval(
        id=uuid.uuid4(),
        intent_id=intent.id,
        run_id=intent.run_id,
        agent_id=intent.agent_id,
        decided_by_user_id=user.id,
        decision=decision_enum,
        reason=(reason or "")[:2000] or None,
        original_policy_reason=intent.policy_reason,
        decided_at=utcnow(),
        final_state=None,
    )
    db.add(approval)
    if decision_enum == ApprovalDecision.APPROVED:
        intent.execution_status = IntentExecutionStatus.APPROVED
        intent.policy_reason = f"approved by {user.email}: {reason or ''}"[:2000]
        ev_type = EventType.INTENT_APPROVED
    else:
        intent.execution_status = IntentExecutionStatus.REJECTED
        intent.policy_reason = f"rejected by {user.email}: {reason or ''}"[:2000]
        intent.executed_at = utcnow()
        approval.final_state = IntentExecutionStatus.REJECTED.value
        ev_type = EventType.INTENT_REJECTED
    if run is not None:
        _emit(
            db,
            intent=intent,
            run=run,
            event=event,
            event_type=ev_type,
            payload={"intent_id": str(intent.id), "intent_type": intent.intent_type, "agent_id": str(intent.agent_id), "decided_by": str(user.id), "reason": (reason or "")[:300]},
            actor_id=user.id,
            notify=decision_enum == ApprovalDecision.APPROVED,  # wake a worker to resume
        )
    db.commit()
    db.refresh(approval)
    return DecisionResult(approval=approval, intent=intent)


_CLAIM_SQL = text(
    """
    WITH candidate AS (
        SELECT id FROM agent_intents
        WHERE execution_status = 'approved'
          AND (resume_lease_expires_at IS NULL OR resume_lease_expires_at < :now)
        ORDER BY created_at
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    UPDATE agent_intents i
       SET resume_worker_id = :worker_id,
           resume_lease_expires_at = :lease_until,
           resume_attempt = i.resume_attempt + 1
      FROM candidate
     WHERE i.id = candidate.id
 RETURNING i.id
    """
)


def claim_next_approved_intent(db: Session, *, worker_id: str, lease_seconds: int, now: Optional[datetime] = None) -> Optional[AgentIntent]:
    """Atomically claim one approved intent for resume. Commits."""
    now = now or utcnow()
    row = db.execute(_CLAIM_SQL, {"now": now, "worker_id": worker_id, "lease_until": now + timedelta(seconds=lease_seconds)}).fetchone()
    db.commit()
    if row is None:
        return None
    intent = db.query(AgentIntent).filter(AgentIntent.id == row[0]).first()
    if intent is not None:
        db.refresh(intent)
    return intent


def _revalidate(intent: AgentIntent) -> ValidatedIntent:
    try:
        itype = IntentType(intent.intent_type)
        payload = PAYLOAD_MODELS[itype].model_validate(intent.payload or {})
        return ValidatedIntent(seq=intent.seq, type_name=intent.intent_type, intent_type=itype, valid=True, payload=payload, raw_payload=intent.payload or {}, idempotency_key=intent.idempotency_key)
    except Exception as exc:  # noqa: BLE001
        return ValidatedIntent(seq=intent.seq, type_name=intent.intent_type, intent_type=None, valid=False, error=str(exc)[:500], raw_payload=intent.payload or {}, idempotency_key=intent.idempotency_key)


def _finish(db: Session, intent: AgentIntent, approval: Optional[IntentApproval], *, status: IntentExecutionStatus, error: Optional[str] = None) -> None:
    intent.execution_status = status
    intent.executed_at = utcnow()
    intent.resume_lease_expires_at = None
    if error:
        intent.error = error[:2000]
    if approval is not None:
        approval.final_state = status.value
        approval.executed_at = utcnow()
        if error:
            approval.resume_error = error[:2000]


def execute_approved_intent(db: Session, intent: AgentIntent, *, settings: SocietySettings, worker_id: str) -> str:
    """Resume ONE approved intent without consulting the model. Commits.
    Returns the terminal execution status value."""
    from ..models import Span

    started = time.monotonic()
    approval = db.query(IntentApproval).filter(IntentApproval.intent_id == intent.id).first()
    run = db.query(AgentRun).filter(AgentRun.id == intent.run_id).first()
    agent = db.query(Agent).filter(Agent.id == intent.agent_id).first()
    grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == intent.agent_id).first()
    event = db.query(SocietyEvent).filter(SocietyEvent.id == run.event_id).first() if run else None

    if _ev(intent.execution_status) != IntentExecutionStatus.APPROVED.value:
        return _ev(intent.execution_status)  # someone else finished it
    if intent.resume_attempt > settings.approval_resume_max_attempts:
        _finish(db, intent, approval, status=IntentExecutionStatus.FAILED, error="resume attempts exhausted (worker kept dying mid-execution)")
        db.commit()
        return IntentExecutionStatus.FAILED.value
    if run is None or agent is None or event is None:
        _finish(db, intent, approval, status=IntentExecutionStatus.FAILED, error="run/agent/event no longer exist")
        db.commit()
        return IntentExecutionStatus.FAILED.value
    if not settings.runtime_enabled:
        # Do not consume the attempt: leave it approved for when the runtime is back on.
        intent.resume_lease_expires_at = None
        intent.resume_attempt = max(0, int(intent.resume_attempt) - 1)
        db.commit()
        return IntentExecutionStatus.APPROVED.value

    if approval is not None and approval.resumed_at is None:
        approval.resumed_at = utcnow()
    _emit(db, intent=intent, run=run, event=event, event_type=EventType.INTENT_RESUMED, payload={"intent_id": str(intent.id), "intent_type": intent.intent_type, "worker_id": worker_id, "attempt": intent.resume_attempt}, actor_type="system")
    db.commit()

    validated = _revalidate(intent)
    verdict = evaluate_intent(validated, grant=grant, settings=settings, agent=agent, approval_granted=True)
    if verdict.decision != PolicyDecision.ALLOW:
        reason = f"re-check at resume failed closed: {verdict.reason}"
        intent.policy_decision = verdict.decision
        intent.policy_reason = reason[:2000]
        _finish(db, intent, approval, status=IntentExecutionStatus.DENIED, error=reason)
        _emit(db, intent=intent, run=run, event=event, event_type=EventType.INTENT_DENIED, payload={"intent_id": str(intent.id), "intent_type": intent.intent_type, "reason": reason[:300], "stage": "resume"}, actor_type="system")
        db.commit()
        return IntentExecutionStatus.DENIED.value

    def heartbeat() -> None:
        intent.resume_lease_expires_at = utcnow() + timedelta(seconds=settings.run_lease_seconds)
        db.commit()

    ctx = ExecContext(db=db, settings=settings, agent=agent, grant=grant, run=run, event=event, intent_row=intent, validated=validated, heartbeat=heartbeat)
    try:
        outcome = execute(ctx)
        intent.result = {"result": outcome.result, "events": outcome.events, "resumed_by": worker_id}
        _finish(db, intent, approval, status=IntentExecutionStatus.EXECUTED)
        db.add(Span(id=uuid.uuid4(), trace_id=run.trace_id or run.correlation_id, span_id=uuid.uuid4(), parent_span_id=run.span_id, agent_id=agent.id, event=f"society.intent.{intent.intent_type}", capability="society", duration_ms=int((time.monotonic() - started) * 1000), status=SpanStatus.SUCCESS, extra_data={"run_id": str(run.id), "intent_id": str(intent.id), "resumed": True}))
        _emit(db, intent=intent, run=run, event=event, event_type=EventType.INTENT_EXECUTED, payload={"intent_id": str(intent.id), "intent_type": intent.intent_type, "events": outcome.events}, actor_type="system")
        db.commit()
        return IntentExecutionStatus.EXECUTED.value
    except ExecutionError as exc:
        db.rollback()
        intent = db.merge(intent)
        approval = db.merge(approval) if approval is not None else None
        _finish(db, intent, approval, status=IntentExecutionStatus.FAILED, error=str(exc))
        db.commit()
        return IntentExecutionStatus.FAILED.value
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        intent = db.merge(intent)
        approval = db.merge(approval) if approval is not None else None
        _finish(db, intent, approval, status=IntentExecutionStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
        db.commit()
        logger.exception("approved intent %s crashed on resume", intent.id)
        return IntentExecutionStatus.FAILED.value


def pending_approvals(db: Session, *, limit: int = 100):
    return (
        db.query(AgentIntent, Agent.name, AgentRun.correlation_id)
        .join(Agent, Agent.id == AgentIntent.agent_id)
        .join(AgentRun, AgentRun.id == AgentIntent.run_id)
        .filter(AgentIntent.execution_status.in_([IntentExecutionStatus.AWAITING_APPROVAL, IntentExecutionStatus.APPROVED]))
        .order_by(AgentIntent.created_at)
        .limit(limit)
        .all()
    )
