"""Durable case operations: open, transition (audited), artifacts, evidence.

Every write to ``repair_cases.state`` goes through :func:`transition`, which
checks the executable state machine, stamps the next action and deadline,
bumps the optimistic ``version`` and appends one ``repair_transitions`` row.
Nothing here commits: callers own the transaction, so a state change and its
audit row land together or not at all.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..society.events import utcnow
from . import state_machine as sm
from .config import MaintenanceSettings
from .orm import MaintenanceIncident, RepairArtifact, RepairCase, RepairEvidence, RepairTransition
from .taxonomy import ActorType, TrustClass


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def state_of(case: RepairCase) -> sm.CaseState:
    return sm.CaseState(case.state)


def open_case(db: Session, incident: MaintenanceIncident, settings: MaintenanceSettings, *, now: Optional[datetime] = None) -> Optional[RepairCase]:
    """Open the ONE active case for an incident. Returns None when another
    worker (or an earlier cycle) already holds it -- the partial unique index
    is the arbiter, not this code."""
    now = now or utcnow()
    case = RepairCase(
        id=uuid.uuid4(),
        incident_id=incident.id,
        state=sm.CaseState.DETECTED.value,
        priority=incident.priority,
        repair_class=incident.incident_class,
        started_at=now,
        state_entered_at=now,
        next_action_at=now,
        deadline_at=sm.deadline_for(sm.CaseState.DETECTED, now),
        case_deadline_at=now + timedelta(hours=settings.case_max_hours),
        model_cost_usd=Decimal("0"),
        facts={},
        escalation={},
        version=0,
        created_at=now,
        updated_at=now,
    )
    sp = db.begin_nested()
    try:
        db.add(case)
        db.flush()
    except IntegrityError:
        sp.rollback()
        return None
    sp.commit()
    incident.case_count = int(incident.case_count or 0) + 1
    db.add(
        RepairTransition(
            case_id=case.id,
            from_state=None,
            to_state=case.state,
            actor_type=ActorType.CONTROLLER.value,
            actor_id="kernel",
            reason_code="incident_open_without_case",
            evidence_digest=incident.current_evidence_digest,
            detail={"incident_id": str(incident.id), "fingerprint": incident.fingerprint, "priority": incident.priority},
            created_at=now,
        )
    )
    db.flush()
    return case


def transition(
    db: Session,
    case: RepairCase,
    dst: sm.CaseState,
    *,
    actor: ActorType,
    actor_id: str,
    reason: str,
    now: Optional[datetime] = None,
    evidence_digest: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
    resumable: Optional[bool] = None,
    escalation: Optional[Dict[str, Any]] = None,
    delay: Optional[timedelta] = None,
) -> None:
    """Move ``case`` to ``dst`` (or re-enter the same state for a retry)."""
    now = now or utcnow()
    src = state_of(case)
    sm.check_transition(src, dst, actor, resumable=bool(case.resumable))
    case.state = dst.value
    case.state_entered_at = now
    case.state_tries = 0
    case.version = int(case.version or 0) + 1
    case.updated_at = now
    if sm.is_terminal(dst):
        case.next_action_at = None
        case.deadline_at = None
        case.terminal_at = now
        case.terminal_reason = reason[:64]
        case.resumable = bool(resumable) if dst is sm.CaseState.SAFELY_ESCALATED else False
        if escalation is not None:
            case.escalation = escalation
        # the lease stays with the worker that holds it; it releases it after
        # its post-step fence (so a terminal write is fenced like any other)
        incident = db.get(MaintenanceIncident, case.incident_id)
        if incident is not None:
            incident.last_case_terminal_at = now
    else:
        # leaving a (resumable) escalation or any other state: live again
        case.terminal_at = None
        case.terminal_reason = None
        case.resumable = False
        # the first step of a new state runs at once; ``poll`` paces waiting (reschedule)
        case.next_action_at = now + (delay if delay is not None else timedelta(0))
        case.deadline_at = sm.deadline_for(dst, now, case_deadline=case.case_deadline_at if dst not in _EXEMPT_FROM_CASE_DEADLINE else None)
        if case.deadline_at is not None and case.next_action_at > case.deadline_at:
            case.next_action_at = case.deadline_at
    db.add(
        RepairTransition(
            case_id=case.id,
            from_state=src.value,
            to_state=dst.value,
            actor_type=actor.value,
            actor_id=actor_id[:128],
            reason_code=reason[:64],
            evidence_digest=evidence_digest,
            detail=detail or {},
            created_at=now,
        )
    )
    db.flush()


#: Once a repair is on main, the case-level wall clock no longer cuts its
#: release/verification short: those states carry their own deadlines, and
#: releasing late is safer than abandoning a merged fix mid-flight.
_EXEMPT_FROM_CASE_DEADLINE = frozenset({sm.CaseState.READY_FOR_RELEASE, sm.CaseState.RELEASING, sm.CaseState.POST_RELEASE_VERIFYING, sm.CaseState.RECOVERY_PENDING})


def reschedule(case: RepairCase, *, now: Optional[datetime] = None, delay: Optional[timedelta] = None) -> None:
    """Stay in the state; come back later (never past the deadline)."""
    now = now or utcnow()
    case.next_action_at = now + (delay if delay is not None else sm.spec(state_of(case)).poll)
    if case.deadline_at is not None and case.next_action_at > case.deadline_at:
        case.next_action_at = case.deadline_at
    case.updated_at = now


def record_artifact(
    db: Session,
    case: RepairCase,
    *,
    kind: str,
    content: Dict[str, Any],
    trust_class: TrustClass,
    produced_by: str,
    plan_revision: int = 0,
    attempt: int = 0,
    now: Optional[datetime] = None,
) -> RepairArtifact:
    """Immutable artifact, idempotent by (case, kind, digest)."""
    d = digest(content)
    existing = db.query(RepairArtifact).filter(RepairArtifact.case_id == case.id, RepairArtifact.kind == kind, RepairArtifact.digest == d).first()
    if existing is not None:
        return existing
    art = RepairArtifact(
        id=uuid.uuid4(),
        case_id=case.id,
        kind=kind[:32],
        digest=d,
        trust_class=trust_class.value,
        plan_revision=plan_revision,
        attempt=attempt,
        produced_by=produced_by[:128],
        content=content,
        created_at=now or utcnow(),
    )
    db.add(art)
    db.flush()
    return art


def record_evidence(
    db: Session,
    case: RepairCase,
    *,
    kind: str,
    source: str,
    collector_version: str,
    trust_class: TrustClass,
    payload: Dict[str, Any],
    now: Optional[datetime] = None,
) -> RepairEvidence:
    if trust_class is TrustClass.MODEL_HYPOTHESIS:
        raise ValueError("model text is never evidence; store it as an artifact")
    d = digest(payload)
    existing = db.query(RepairEvidence).filter(RepairEvidence.case_id == case.id, RepairEvidence.kind == kind, RepairEvidence.digest == d).first()
    if existing is not None:
        return existing
    ev = RepairEvidence(
        id=uuid.uuid4(),
        case_id=case.id,
        kind=kind[:48],
        source=source[:64],
        collector_version=collector_version[:32],
        trust_class=trust_class.value,
        digest=d,
        payload=payload,
        collected_at=now or utcnow(),
    )
    db.add(ev)
    db.flush()
    return ev


def merge_facts(case: RepairCase, **facts: Any) -> None:
    """Controller facts about the case (JSONB). Reassigned so SQLAlchemy sees it."""
    f = dict(case.facts or {})
    f.update(facts)
    case.facts = f


__all__ = [
    "canonical",
    "digest",
    "state_of",
    "open_case",
    "transition",
    "reschedule",
    "record_artifact",
    "record_evidence",
    "merge_facts",
]
