"""A READY candidate either gets a promotion or a structured reason (staging 2026-10-27, feb64aa6).

The Governor requests a PR for a READY candidate (REQUEST_PR_PROMOTION); the controller decides.
When that request is refused -- a change budget is spent (``SOCIETY_MAX_PROMOTIONS_PER_DAY``,
``SOCIETY_MAX_OPEN_AUTONOMOUS_PRS``), or nobody asked -- nothing ever asked again, and the candidate
sat READY with no PR and no word why.

Now, deterministically:
- :func:`ready_backlog` names, for every READY candidate without an active promotion, ONE reason
  code (our words): ``promotion_<status>`` (a promotion ended, e.g. the owner closed the PR),
  ``provider_disabled``, ``stale_base`` (its base is no longer an ancestor of main: it cannot be
  promoted and needs a rebuild), ``cap_promotions_per_day``, ``cap_open_prs``, ``refused`` (the
  last request's refusal) or ``awaiting_request``; with how far its base is behind main;
- :func:`retry_deferred` re-wakes the Governor for the oldest such candidates while there is
  capacity, at most once per candidate per UTC day, with the original READY payload in a fresh
  system story. It adds no authority: the same role gets the same wake, and the request still goes
  through policy, the change budgets and the controller. A candidate whose promotion ended is
  never re-offered (the owner's close stands);
- :func:`settle_ended` moves a READY row whose promotion already ended to its terminal status
  (MERGED, or REJECTED when the owner closed the PR) -- the controller does this on every new end
  (promotion._settle_candidate); this catches rows that ended before it did (staging 2026-10-29:
  4 of 6 "READY" were merged or closed);
- :func:`caps` names the change budget in effect (for the heartbeat, the report and the validator)."""

from __future__ import annotations

import logging
import pathlib
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models import AgentIntent, CodeCandidate, CodeCandidateStatus, CodePromotion, PromotionStatus, SocietyEvent
from .config import SocietySettings
from .engineering import workspace as ws_mod
from .events import EventType, emit_event, utcnow
from .promotion import ACTIVE_STATUSES, CLOSED_BY_OWNER, active_promotion_for, open_prs, promotions_today

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


def caps(db: Session, settings: SocietySettings, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The change budget in effect and what is spent of it (numbers only)."""
    return {"max_promotions_per_day": settings.max_promotions_per_day, "promotions_today": promotions_today(db, now),
            "max_open_autonomous_prs": settings.max_open_autonomous_prs, "open_prs": open_prs(db),
            "provider": settings.promotion_provider, "auto_merge_enabled": bool(settings.auto_merge_enabled)}


#: a promotion that ended with one of these failure reasons was closed by the owner (older rows say the latter)
_CLOSED_REASONS = (CLOSED_BY_OWNER, "pull request closed without merge")


def settle_ended(db: Session) -> int:
    """READY rows whose LATEST promotion already ended: MERGED -> MERGED, closed by the owner -> REJECTED.
    Status and reason only (no task, wallet or event). Commits when it changed a row."""
    n = 0
    for cand in db.query(CodeCandidate).filter(CodeCandidate.status == CodeCandidateStatus.READY).all():
        last = db.query(CodePromotion).filter(CodePromotion.candidate_id == cand.id).order_by(CodePromotion.created_at.desc()).first()
        if last is None:
            continue
        status = _ev(last.status)
        if status == PromotionStatus.MERGED.value:
            cand.status = CodeCandidateStatus.MERGED
        elif status == PromotionStatus.REJECTED.value and (last.failure_reason or "").startswith(_CLOSED_REASONS):
            cand.status = CodeCandidateStatus.REJECTED
            cand.error = f"{CLOSED_BY_OWNER}: pull request closed without merge (PR #{last.external_pr_number or '?'})"
        else:
            continue
        n += 1
        logger.info("promotion queue: candidate %s settled as %s (promotion %s %s)", str(cand.id)[:8], _ev(cand.status), str(last.id)[:8], status)
    if n:
        db.commit()
    return n


def _base(settings: SocietySettings, base_sha: Optional[str]) -> Dict[str, Any]:
    """Where a candidate's base stands against main in the worker's checkout: ``{"ancestor", "behind"}``,
    or {} where there is no checkout (the API process) or git cannot say."""
    root = pathlib.Path(settings.repo_root or "")
    if not base_sha or not (root / ".git").exists():
        return {}
    try:
        ws_mod._git(["cat-file", "-e", f"{base_sha}^{{commit}}"], cwd=root)
    except ws_mod.WorkspaceError:
        return {}  # a commit this checkout does not have says nothing about main
    try:
        ws_mod._git(["merge-base", "--is-ancestor", base_sha, "HEAD"], cwd=root)
    except ws_mod.WorkspaceError:
        return {"ancestor": False, "behind": None}
    try:
        return {"ancestor": True, "behind": int(ws_mod._git(["rev-list", "--count", f"{base_sha}..HEAD"], cwd=root).strip() or 0)}
    except (ws_mod.WorkspaceError, ValueError):
        return {"ancestor": True, "behind": None}


def worker_caps(db: Session) -> Optional[Dict[str, Any]]:
    """The change budget the WORKER last reported (``change_budget`` of its newest heartbeat) -- the
    process that promotes. The registry API runs with its own env, so its settings may say
    ``provider=disabled`` while the worker promotes (Owner Console 2026-10-29)."""
    row = (db.query(SocietyEvent.payload).filter(SocietyEvent.event_type.in_([EventType.SOCIETY_HEARTBEAT, EventType.SOCIETY_HEARTBEAT_IDLE]))
           .order_by(SocietyEvent.created_at.desc()).first())
    cb = (row[0] or {}).get("change_budget") if row else None
    return cb if isinstance(cb, dict) and cb.get("provider") else None


def _as_worker(settings: SocietySettings, cb: Optional[Dict[str, Any]]) -> SocietySettings:
    """``settings`` with the worker's promotion provider and change-budget caps (when it reported them)."""
    if not cb:
        return settings
    import copy  # noqa: PLC0415

    # A copy, NOT dataclasses.replace: this process's env need not satisfy the worker's provider
    # rules (e.g. SOCIETY_GITHUB_REPOSITORY), and these settings only count and label, never promote.
    view = copy.copy(settings)
    object.__setattr__(view, "promotion_provider", str(cb["provider"]))
    for k in ("max_promotions_per_day", "max_open_autonomous_prs"):
        if isinstance(cb.get(k), int):
            object.__setattr__(view, k, cb[k])
    return view


def worker_view_caps(db: Session, settings: SocietySettings, now: Optional[datetime] = None) -> Dict[str, Any]:
    """:func:`caps` as the worker sees it: its provider, caps and auto-merge, counted now."""
    cb = worker_caps(db)
    out = caps(db, _as_worker(settings, cb), now)
    if cb and "auto_merge_enabled" in cb:
        out["auto_merge_enabled"] = bool(cb["auto_merge_enabled"])
    return out


def ready_backlog(db: Session, settings: SocietySettings, now: Optional[datetime] = None, *, from_worker: bool = False) -> List[Dict[str, Any]]:
    """Every READY candidate without an active promotion, oldest first, with one reason code.
    ``from_worker``: judge with the worker's provider and caps (its last heartbeat), not this process's."""
    now = now or utcnow()
    if from_worker:
        settings = _as_worker(settings, worker_caps(db))
    cap = capacity(db, settings, now)
    out = []
    for cand in db.query(CodeCandidate).filter(CodeCandidate.status == CodeCandidateStatus.READY).order_by(CodeCandidate.updated_at).all():
        if active_promotion_for(db, cand.id) is not None:
            continue
        ended = db.query(CodePromotion).filter(CodePromotion.candidate_id == cand.id, CodePromotion.status.notin_(ACTIVE_STATUSES)).order_by(CodePromotion.created_at.desc()).first()
        req = _last_request(db, cand.id)
        base = _base(settings, cand.base_sha)
        detail = ""
        if ended is not None:
            reason, detail = f"promotion_{_ev(ended.status)}", (ended.failure_reason or "")[:200]
        elif settings.promotion_provider == "disabled":
            reason = "provider_disabled"
        elif base.get("ancestor") is False:
            reason, detail = "stale_base", f"base {str(cand.base_sha)[:10]} is not an ancestor of main: rebuild on the current base"
        elif cap["promotions_left_today"] <= 0:
            reason, detail = "cap_promotions_per_day", f"SOCIETY_MAX_PROMOTIONS_PER_DAY={settings.max_promotions_per_day}"
        elif cap["open_pr_slots"] <= 0:
            reason, detail = "cap_open_prs", f"SOCIETY_MAX_OPEN_AUTONOMOUS_PRS={settings.max_open_autonomous_prs}"
        elif req is not None and _ev(req.execution_status) in ("failed", "denied"):
            reason, detail = "refused", (req.error or req.policy_reason or "")[:200]
        else:
            reason = "awaiting_request"
        out.append({"candidate": str(cand.id)[:8], "candidate_id": str(cand.id), "title": cand.title, "reason": reason, "detail": detail,
                    "ready_since": cand.updated_at.isoformat() if cand.updated_at else None, "requests": _request_count(db, cand.id),
                    "base_behind_main": base.get("behind")})
    return out


def _request_count(db: Session, cand_id) -> int:
    return int(db.query(AgentIntent).filter(AgentIntent.intent_type == "REQUEST_PR_PROMOTION", AgentIntent.payload["candidate_id"].astext == str(cand_id)).count())


def ready_view(db: Session, settings: SocietySettings, now: Optional[datetime] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """READY candidates with their latest promotion and PR link, or the reason there is none (Owner
    Console), judged with the worker's provider and caps."""
    waiting = {b["candidate_id"]: b for b in ready_backlog(db, settings, now, from_worker=True)}
    out = []
    for cand in db.query(CodeCandidate).filter(CodeCandidate.status == CodeCandidateStatus.READY).order_by(CodeCandidate.updated_at.desc()).limit(limit).all():
        last = db.query(CodePromotion).filter(CodePromotion.candidate_id == cand.id).order_by(CodePromotion.created_at.desc()).first()
        b = waiting.get(str(cand.id)) or {}
        out.append({"candidate": str(cand.id)[:8], "candidate_id": str(cand.id), "title": cand.title, "risk_tier": _ev(cand.risk_tier) if cand.risk_tier else None,
                    "promotion": _ev(last.status) if last is not None else None, "pr_number": last.external_pr_number if last is not None else None,
                    "pr_url": last.external_pr_url if last is not None else None, "reason": b.get("reason"), "detail": b.get("detail")})
    return out


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


__all__ = ["capacity", "caps", "ready_backlog", "ready_view", "retry_deferred", "settle_ended", "worker_caps", "worker_view_caps"]
