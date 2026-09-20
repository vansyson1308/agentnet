"""Operator remedy for a candidate that can never finish.

A candidate is only ever moved by a role that wakes, and the loop breaker can
legitimately swallow the event that would have woken it. Before this module the
result was permanent: nothing re-emitted the wake, no route could close the row,
and the Scout then CORRECTLY declined to re-propose work that already had an
open candidate. Three defensible behaviours composing into a deadlock.

Abandoning is an OPERATOR act, never an agent one. There is deliberately no
intent type: a society that can retire its own unfinished work can also retire
the evidence that it failed. The row is kept, the reason is required, and the
in-flight implementation task is closed through the ordinary escrow path so the
money moves exactly once.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .. import task_service
from ..models import CodeCandidate, CodeCandidateStatus, CodePromotion, PromotionStatus, TaskSession, TaskStatus, User
from .events import EventType, emit_event

MAX_REASON_CHARS = 1000

#: Work that is finished, one way or another. Abandoning it would rewrite history.
TERMINAL_STATUSES = (
    CodeCandidateStatus.REJECTED,
    CodeCandidateStatus.FAILED,
)
#: A promotion in any of these states put the change somewhere real.
PROMOTED_STATUSES = (PromotionStatus.MERGED.value,)


class AbandonRefused(Exception):
    """The candidate must not be abandoned; the message says why."""


@dataclass
class AbandonResult:
    candidate: CodeCandidate
    already_abandoned: bool
    task_refunded: bool
    reason: str


def _ev(v) -> str:
    return v.value if hasattr(v, "value") else str(v)


def abandon(
    db: Session,
    *,
    candidate_id: uuid.UUID,
    reason: str,
    operator: User,
) -> AbandonResult:
    """Close a candidate that cannot progress. Idempotent; never deletes."""
    reason = (reason or "").strip()
    if not reason:
        raise AbandonRefused("a reason is required: an abandoned candidate must say why")
    if len(reason) > MAX_REASON_CHARS:
        raise AbandonRefused(f"reason exceeds {MAX_REASON_CHARS} characters")

    row = db.query(CodeCandidate).filter(CodeCandidate.id == candidate_id).with_for_update().first()
    if row is None:
        raise LookupError("candidate not found")

    status = _ev(row.status)
    if status == CodeCandidateStatus.ABANDONED.value:
        # Idempotent: no second event, no second refund, no rewritten reason.
        return AbandonResult(candidate=row, already_abandoned=True, task_refunded=False, reason=row.error or reason)
    if status in [s.value for s in TERMINAL_STATUSES]:
        raise AbandonRefused(f"candidate is {status}; it is already closed and abandoning would rewrite that")

    promo = (
        db.query(CodePromotion)
        .filter(CodePromotion.candidate_id == row.id, CodePromotion.status.in_(PROMOTED_STATUSES))
        .first()
    )
    if promo is not None:
        raise AbandonRefused("candidate was merged through a promotion; abandoning it would misdescribe main")

    # Economics: close the implementation task through the ordinary escrow path.
    # Never touch a wallet here — fail_task_with_refund takes the row locks and
    # releases the reservation exactly once, and a task already in a terminal
    # state is left alone so a repeat cannot double-release.
    refunded = False
    if row.task_id is not None:
        task = db.query(TaskSession).filter(TaskSession.id == row.task_id).first()
        if task is not None and _ev(task.status) in (TaskStatus.INITIATED.value, TaskStatus.IN_PROGRESS.value):
            task_service.fail_task_with_refund(
                db=db,
                task_id=row.task_id,
                error_message=f"candidate abandoned by operator: {reason}"[:500],
            )
            refunded = True

    row.status = CodeCandidateStatus.ABANDONED
    row.error = reason
    db.flush()

    emit_event(
        db,
        event_type=EventType.CODE_CANDIDATE_ABANDONED,
        payload={
            "candidate_id": str(row.id),
            "previous_status": status,
            "reason": reason,
            "task_refunded": refunded,
            # The operator is identified by actor_id below. Their email never
            # enters a payload -- payloads are read back by operators and tests.
        },
        actor_type="operator",
        actor_id=operator.id,
        subject_type="code_candidate",
        subject_id=row.id,
        correlation_id=row.correlation_id,
        idempotency_key=f"candidate-abandoned:{row.id}",
        notify=True,
    )
    return AbandonResult(candidate=row, already_abandoned=False, task_refunded=refunded, reason=reason)


__all__ = ["abandon", "AbandonRefused", "AbandonResult", "MAX_REASON_CHARS", "TERMINAL_STATUSES"]
