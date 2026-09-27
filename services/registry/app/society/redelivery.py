"""Re-deliver a candidate wake the loop breaker swallowed: once, in a fresh story.

The loop breaker ends a story after ``SOCIETY_MAX_RUNS_PER_CORRELATION`` runs.
That is the right answer to a runaway conversation. It is the wrong answer to
a code candidate whose next stage owner was about to be woken:
- the wake is marked IGNORED;
- nothing else re-emits it;
- the candidate waits forever in a stage no role will ever look at again.

Staging, three times:
- 9da14a08 stayed REQUESTED after correlation 56307641 hit 12 runs, until an
  operator abandoned it;
- f8296297 likewise ended with an operator abandon;
- 23ac830a's ``code_candidate.built`` was ignored in correlation 4017ce48, so QA
  never ran until the Builder's hourly heartbeat asked for it.

The Builder's heartbeat covers only its own stages. Nothing re-wakes Security
for ``security_review``, or the Governor for ``ready``.

The remedy is deliberately narrow:
- only the candidate lifecycle wakes in :data:`REDELIVERABLE`;
- only while the candidate still sits in the stage that wake was for;
- a re-delivery carries ``redelivered_from`` and is never re-delivered, so each
  swallowed wake is re-sent at most once (idempotency key
  ``redeliver:<event id>``) and the state machine bounds the total;
- the new event is a fresh story (new correlation, depth 0) from the system,
  with the original payload.

It adds no authority: the same role is woken with the same payload it would
have received, and every guard (policy, grants, QA, Security, promotion) still
decides.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from sqlalchemy import String, and_, cast, exists, literal, or_
from sqlalchemy.orm import Session, aliased

from ..models import CodeCandidate, CodeCandidateStatus, CodePromotion, SocietyEvent, SocietyEventStatus
from .config import SocietySettings
from .events import EventType, emit_event, utcnow

logger = logging.getLogger("agentnet.society.redelivery")

#: Lifecycle wake -> the candidate statuses in which that wake is still owed.
REDELIVERABLE: Dict[str, Tuple[CodeCandidateStatus, ...]] = {
    EventType.CODE_CHANGE_REQUESTED: (CodeCandidateStatus.REQUESTED,),
    EventType.CODE_CANDIDATE_BUILT: (CodeCandidateStatus.BUILT,),
    EventType.CODE_CANDIDATE_QA_FAILED: (CodeCandidateStatus.QA_FAILED,),
    EventType.CODE_CANDIDATE_SECURITY_REVIEW: (CodeCandidateStatus.SECURITY_REVIEW,),
    EventType.CODE_CANDIDATE_READY: (CodeCandidateStatus.READY,),
}
#: Payload key marking a re-delivery; a re-delivery is never re-delivered.
REDELIVERED_FROM = "redelivered_from"
#: Idempotency-key prefix of a re-delivery (``redeliver:<original event id>``).
KEY_PREFIX = "redeliver:"
#: Swallowed wakes older than this are left alone (the candidate has moved on
#: or an operator will see it in the stranded-candidate view).
LOOKBACK = timedelta(hours=24)
#: Upper bound on re-deliveries per dispatch cycle.
MAX_PER_SWEEP = 5


def _owed():
    """The SQL form of "the candidate still waits for this wake": the candidate
    sits in a stage the wake was for, and a READY wake has no promotion yet."""
    promoted = exists().where(CodePromotion.candidate_id == CodeCandidate.id)
    clauses = []
    for event_type, statuses in REDELIVERABLE.items():
        clause = and_(SocietyEvent.event_type == event_type, CodeCandidate.status.in_(list(statuses)))
        if event_type == EventType.CODE_CANDIDATE_READY:
            clause = and_(clause, ~promoted)
        clauses.append(clause)
    return or_(*clauses)


def redeliver_swallowed_wakes(db: Session, settings: SocietySettings, now: Optional[datetime] = None) -> int:
    """Re-emit each swallowed, still-owed candidate wake once. Commits when it
    emitted anything; returns how many wakes were re-delivered.

    Every condition is in the query, so a wake that is no longer owed, or was
    already re-delivered, never takes a slot from one that is."""
    now = now or utcnow()
    resent = aliased(SocietyEvent)
    already_resent = exists().where(resent.idempotency_key == literal(KEY_PREFIX) + cast(SocietyEvent.id, String))
    swallowed = (
        db.query(SocietyEvent)
        .join(CodeCandidate, CodeCandidate.id == SocietyEvent.subject_id)
        .filter(
            SocietyEvent.status == SocietyEventStatus.IGNORED,
            SocietyEvent.dispatch_note.like("loop breaker%"),
            SocietyEvent.event_type.in_(list(REDELIVERABLE)),
            SocietyEvent.subject_type == "code_candidate",
            SocietyEvent.created_at >= now - LOOKBACK,
            # a re-delivery that was swallowed again stays swallowed
            or_(SocietyEvent.idempotency_key.is_(None), ~SocietyEvent.idempotency_key.like(KEY_PREFIX + "%")),
            ~already_resent,
            _owed(),
        )
        .order_by(SocietyEvent.created_at)
        .limit(MAX_PER_SWEEP)
        .all()
    )
    sent = 0
    for ev in swallowed:
        payload = dict(ev.payload or {})
        if payload.get(REDELIVERED_FROM):
            continue
        new = emit_event(
            db,
            event_type=ev.event_type,
            payload={**payload, REDELIVERED_FROM: str(ev.id)},
            actor_type="system",
            subject_type=ev.subject_type,
            subject_id=ev.subject_id,
            idempotency_key=f"{KEY_PREFIX}{ev.id}",
        )
        if getattr(new, "deduplicated", False):
            continue
        sent += 1
        logger.info("re-delivered swallowed %s for candidate %s as %s", ev.event_type, ev.subject_id, new.id)
    if sent:
        db.commit()
    return sent


__all__ = ["REDELIVERABLE", "REDELIVERED_FROM", "redeliver_swallowed_wakes"]
