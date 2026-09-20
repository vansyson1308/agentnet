"""Audited correction of a memory whose claim turned out to be false.

Memory is evidence, not policy — but evidence that is wrong still steers the
fleet. Phase 5 showed how: a Scout's ``CREATE_IMPROVEMENT`` was refused for a
schema violation, the same run's ``WRITE_MEMORY`` executed normally and
recorded "improvement raised", and because that memory came from a real signal
it never expired. Three later runs declined the same signal as already handled,
each writing another note corroborating the first. The belief was false from
the first minute and grew more confident with every repetition.

Refutation is how a society corrects its record without falsifying it:

* the row is NEVER deleted and its content, provenance and timestamps are
  never touched — a refuted belief stays queryable, because "we believed this
  and were wrong" is itself evidence;
* ``validation_state`` becomes ``refuted``, which ``context.memory_rank``
  already demotes hard (3-day half-life, -0.5 score);
* an append-only ``memory_validation_events`` row records who, when, why and
  on what evidence, enforced append-only by a database trigger;
* a causation-linked ``memory.refuted`` event is emitted exactly once.

Authority: trusted code only. ``operator`` comes from the one operator
dependency (user JWTs only); ``evaluator`` is reserved for trusted evaluation
machinery that disproves a belief objectively. There is deliberately NO intent
type for this: a model can neither validate nor refute its own memory, because
a system that can grade its own evidence has no evidence.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from ..models import MemoryItem, MemoryValidationEvent, User
from .events import EventType, emit_event

logger = logging.getLogger(__name__)

VALID_STATES = ("unvalidated", "validated", "refuted")
ACTOR_TYPES = ("operator", "evaluator")
MAX_REASON_CHARS = 1000


class MemoryNotFound(Exception):
    """No such memory row."""


@dataclass
class RefutationResult:
    memory: MemoryItem
    record: Optional[MemoryValidationEvent]
    already_refuted: bool


def refute(
    db: Session,
    *,
    memory_id: uuid.UUID,
    reason: str,
    actor_type: str = "operator",
    actor: Optional[User] = None,
    evidence: Optional[Dict[str, Any]] = None,
    superseded_by: Optional[uuid.UUID] = None,
) -> RefutationResult:
    """Mark one memory ``refuted``. Idempotent. Does NOT commit.

    Repeating the call on an already-refuted row is a no-op: no second history
    row, no second event, no changed timestamps.
    """
    if actor_type not in ACTOR_TYPES:
        raise ValueError(f"actor_type must be one of {ACTOR_TYPES}")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a refutation must carry a reason")
    if len(reason) > MAX_REASON_CHARS:
        reason = reason[:MAX_REASON_CHARS]

    row = (
        db.query(MemoryItem)
        .filter(MemoryItem.id == memory_id)
        .with_for_update()
        .first()
    )
    if row is None:
        raise MemoryNotFound(str(memory_id))

    previous = (row.validation_state or "unvalidated").lower()
    if previous == "refuted":
        return RefutationResult(memory=row, record=None, already_refuted=True)

    row.validation_state = "refuted"
    if superseded_by is not None:
        row.superseded_by = superseded_by

    record = MemoryValidationEvent(
        id=uuid.uuid4(),
        memory_id=row.id,
        from_state=previous,
        to_state="refuted",
        actor_type=actor_type,
        actor_user_id=getattr(actor, "id", None),
        reason=reason,
        evidence=evidence or {},
    )
    db.add(record)
    db.flush()

    emit_event(
        db,
        event_type=EventType.MEMORY_REFUTED,
        payload={
            "memory_id": str(row.id),
            "from_state": previous,
            "to_state": "refuted",
            "actor_type": actor_type,
            "reason": reason,
        },
        subject_type="memory",
        subject_id=row.id,
        correlation_id=row.correlation_id,
        idempotency_key=f"memory-refuted:{row.id}",
    )
    logger.info("memory %s refuted by %s", row.id, actor_type)
    return RefutationResult(memory=row, record=record, already_refuted=False)


def history(db: Session, memory_id: uuid.UUID, limit: int = 20):
    """Newest-first validation history for one memory (audit surface)."""
    return (
        db.query(MemoryValidationEvent)
        .filter(MemoryValidationEvent.memory_id == memory_id)
        .order_by(MemoryValidationEvent.created_at.desc())
        .limit(limit)
        .all()
    )
