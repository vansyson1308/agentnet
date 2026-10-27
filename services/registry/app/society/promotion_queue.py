"""A READY candidate either gets a promotion or a structured reason (staging 2026-10-27, feb64aa6).

The Governor requests a PR for a READY candidate (REQUEST_PR_PROMOTION); the controller decides.
When that request is refused -- a change budget is spent (``SOCIETY_MAX_PROMOTIONS_PER_DAY``,
``SOCIETY_MAX_OPEN_AUTONOMOUS_PRS``), or nobody asked -- nothing ever asked again, and the candidate
sat READY with no PR and no word why.

Now, deterministically:
- :func:`ready_backlog` names, for every READY candidate without an active promotion, ONE reason
  code (our words): ``promotion_<status>`` (a promotion ended, e.g. the owner closed the PR),
  ``provider_disabled``, ``cap_promotions_per_day``, ``cap_open_prs``, ``refused`` (the last
  request's refusal) or ``awaiting_request``;
- :func:`retry_deferred` re-wakes the Governor for the oldest such candidates while there is
  capacity, at most once per candidate per UTC day, with the original READY payload in a fresh
  system story. It adds no authority: the same role gets the same wake, and the request still goes
  through policy, the change budgets and the controller. A candidate whose promotion ended is
  never re-offered (the owner's close stands)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models import AgentIntent, CodeCandidate, CodeCandidateStatus, CodePromotion, SocietyEvent
from .config import SocietySettings
from .events import EventType, emit_event, utcnow
from .promotion import ACTIVE_STATUSES, active_promotion_for, open_prs, promotions_today

logger = logging.getLogger("agentnet.society.promotion_queue")

#: a READY wake gets this long to produce a request before it is re-offered
RETRY_AFTER = timedelta(hours=1)
KEY_PREFIX = "promote-retry:"
CAP_MARK = "change budget exhausted"


def _ev(v: Any) -> str:
    return getattr(v, "value", v)


def _last_request(db: Session, cand_id) -> Optional[AgentIntent]:
    return (db.query(AgentIntent).filter(AgentIntent.intent_type == "REQUEST_PR_PROMOTION", AgentIntent.payload["candidate_id"].astext == str(cand_id))
            .order_by(AgentIntent.created_at.desc()).first())


def _last_wake(db: Session, cand_id) -> Optional[SocietyEvent]:
    return (db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.CODE_CANDIDATE_READY, SocietyEvent.subject_id == cand_id)
            .order_by(SocietyEvent.created_at.desc()).first())


def capacity(db: Session, settings: SocietySettings, now: Optional[datetime] = None) -> Dict[str, int]:
    return {"promotions_left_today": max(0, settings.max_promotions_per_day - promotions_today(db, now)),
            "open_pr_slots": max(0, settings.max_open_autonomous_prs - open_prs(db))}


def ready_backlog(db: Session, settings: SocietySettings, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Every READY candidate without an active promotion, oldest first, with one reason code."""
    now = now or utcnow()
    cap = capacity(db, settings, now)
    out = []
    for cand in db.query(CodeCandidate).filter(CodeCandidate.status == CodeCandidateStatus.READY).order_by(CodeCandidate.updated_at).all():
        if active_promotion_for(db, cand.id) is not None:
            continue
        ended = db.query(CodePromotion).filter(CodePromotion.candidate_id == cand.id, CodePromotion.status.notin_(ACTIVE_STATUSES)).order_by(CodePromotion.created_at.desc()).first()
        req = _last_request(db, cand.id)
        detail = ""
        if ended is not None:
            reason, detail = f"promotion_{_ev(ended.status)}", (ended.failure_reason or "")[:200]
        elif settings.promotion_provider == "disabled":
            reason = "provider_disabled"
        elif cap["promotions_left_today"] <= 0:
            reason, detail = "cap_promotions_per_day", f"SOCIETY_MAX_PROMOTIONS_PER_DAY={settings.max_promotions_per_day}"
        elif cap["open_pr_slots"] <= 0:
            reason, detail = "cap_open_prs", f"SOCIETY_MAX_OPEN_AUTONOMOUS_PRS={settings.max_open_autonomous_prs}"
        elif req is not None and _ev(req.execution_status) in ("failed", "denied"):
            reason, detail = "refused", (req.error or req.policy_reason or "")[:200]
        else:
            reason = "awaiting_request"
        out.append({"candidate": str(cand.id)[:8], "candidate_id": str(cand.id), "title": cand.title, "reason": reason, "detail": detail,
                    "ready_since": cand.updated_at.isoformat() if cand.updated_at else None, "requests": _request_count(db, cand.id)})
    return out


def _request_count(db: Session, cand_id) -> int:
    return int(db.query(AgentIntent).filter(AgentIntent.intent_type == "REQUEST_PR_PROMOTION", AgentIntent.payload["candidate_id"].astext == str(cand_id)).count())


def retry_deferred(db: Session, settings: SocietySettings, now: Optional[datetime] = None) -> int:
    """Re-offer the oldest waiting READY candidates to the Governor while capacity lasts. Commits when it emitted."""
    now = now or utcnow()
    cap = capacity(db, settings, now)
    slots = min(cap["promotions_left_today"], cap["open_pr_slots"])
    sent = 0
    for item in ready_backlog(db, settings, now):
        if sent >= slots:
            break
        if item["reason"] not in ("awaiting_request", "refused"):
            continue
        last = _last_wake(db, item["candidate_id"])
        if last is not None and last.created_at > now - RETRY_AFTER:
            continue  # the Governor still has its wake
        payload = {k: v for k, v in dict((last.payload if last is not None else None) or {}).items() if k not in ("redelivered_from", "promotion_retry")}
        payload.setdefault("candidate_id", item["candidate_id"])
        new = emit_event(db, event_type=EventType.CODE_CANDIDATE_READY, payload={**payload, "promotion_retry": item["reason"]}, actor_type="system",
                         subject_type="code_candidate", subject_id=uuid.UUID(item["candidate_id"]),
                         idempotency_key=f"{KEY_PREFIX}{item['candidate_id']}:{now.date().isoformat()}")
        if getattr(new, "deduplicated", False):
            continue
        sent += 1
        logger.info("promotion retry: candidate %s re-offered to the Governor (%s)", item["candidate"], item["reason"])
    if sent:
        db.commit()
    return sent


__all__ = ["capacity", "ready_backlog", "retry_deferred"]
