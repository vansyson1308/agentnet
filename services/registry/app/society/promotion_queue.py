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
  capacity, once per candidate per capacity change (:func:`capacity_token`: a cap raised, a
  promotion ended, a new UTC day), with the original READY payload in a fresh system story. It adds no authority: the same role gets the same wake, and the request still goes
  through policy, the change budgets and the controller. A candidate whose promotion ended is
  never re-offered (the owner's close stands);
- :func:`settle_ended` moves a READY row whose promotion already ended to its terminal status
  (MERGED, or REJECTED when the owner closed the PR) -- the controller does this on every new end
  (promotion._settle_candidate); this catches rows that ended before it did (staging 2026-10-29:
  4 of 6 "READY" were merged or closed);
- :func:`settle_reoffers` (owner 2026-10-29 G1v, "READY -> PR must actually happen"): once a re-offer's
  story ended, records WHY no request came (``governor_did_not_request``, ``governor_run_<status>``,
  ``governor_request_refused``, ...) as ``promotion.reoffer_settled``; with
  ``SOCIETY_READY_FALLBACK_ENABLED`` and free capacity, the runtime then requests the promotion itself
  on the Governor's own grant -- the same policy verdict (``policy.evaluate_intent``), the same change
  budgets and the same controller as the Governor's REQUEST_PR_PROMOTION; a refusal is recorded, never
  retried around;
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
        elif req is not None and _ev(req.execution_status) in ("failed", "denied") and not _settled_after(db, cand.id, req.created_at):
            reason, detail = "refused", (req.error or req.policy_reason or "")[:200]
        else:
            settled = _last_settled(db, cand.id)
            if settled is not None and (req is None or settled.created_at >= req.created_at):
                reason, detail = "not_requested", f"{settled.payload.get('reason')}: {settled.payload.get('fallback')}"[:200]
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


def _last_settled(db: Session, cand_id) -> Optional[SocietyEvent]:
    return (db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.READY_REOFFER_SETTLED, SocietyEvent.subject_id == cand_id)
            .order_by(SocietyEvent.created_at.desc()).first())


def _settled_after(db: Session, cand_id, when: datetime) -> bool:
    last = _last_settled(db, cand_id)
    return last is not None and last.created_at >= when


SETTLE_PREFIX = "reoffer-settled:"
#: the reasons a re-offer produced no request that leave the decision to the runtime (nobody said no)
FALLBACK_REASONS = ("governor_did_not_request", "governor_not_woken", "governor_run_skipped", "governor_run_dead")
_OPEN_RUN = ("queued", "claimed", "running", "failed")


def _why_not(db: Session, event: SocietyEvent, cand: CodeCandidate) -> tuple:
    """(reason, detail, the Governor's run) for a finished re-offer story. Structural codes only."""
    from ..models import AgentRun  # noqa: PLC0415

    runs = db.query(AgentRun).filter(AgentRun.event_id == event.id, AgentRun.role == "governor").order_by(AgentRun.created_at).all()
    if not runs:
        return "governor_not_woken", f"event {_ev(event.status)}", None
    asks = (db.query(AgentIntent).filter(AgentIntent.run_id.in_([r.id for r in runs]), AgentIntent.intent_type == "REQUEST_PR_PROMOTION",
                                         AgentIntent.payload["candidate_id"].astext == str(cand.id)).all())
    if any(_ev(a.execution_status) == "executed" for a in asks):
        return "requested", "", runs[-1]
    if asks:
        a = asks[-1]
        return "governor_request_refused", (a.error or a.policy_reason or "")[:200], runs[-1]
    last = runs[-1]
    if _ev(last.status) != "completed":
        return f"governor_run_{_ev(last.status)}", (last.error or "")[:200], last
    return "governor_did_not_request", "", last


def _fallback(db: Session, settings: SocietySettings, cand: CodeCandidate, run: Any, event: SocietyEvent, reason: str, now: datetime) -> tuple:
    """The runtime's request on the Governor's grant: (outcome, promotion id or None)."""
    from ..models import Agent, AgentCapabilityGrant  # noqa: PLC0415
    from . import policy  # noqa: PLC0415
    from .intents import PAYLOAD_MODELS, IntentType, ValidatedIntent  # noqa: PLC0415
    from .promotion import request_promotion  # noqa: PLC0415

    if not settings.ready_fallback_enabled:
        return "off", None
    if settings.promotion_provider == "disabled":
        return "provider_disabled", None
    cap = capacity(db, settings, now)
    if min(cap["promotions_left_today"], cap["open_pr_slots"]) <= 0:
        return "no_capacity", None
    if _base(settings, cand.base_sha).get("ancestor") is False:
        return "stale_base", None
    if run is None:
        return "no_governor_run", None
    grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == run.agent_id).first()
    agent = db.query(Agent).filter(Agent.id == run.agent_id).first()
    intent = ValidatedIntent(seq=0, type_name=IntentType.REQUEST_PR_PROMOTION.value, intent_type=IntentType.REQUEST_PR_PROMOTION, valid=True,
                             payload=PAYLOAD_MODELS[IntentType.REQUEST_PR_PROMOTION](candidate_id=cand.id), idempotency_key=f"{SETTLE_PREFIX}{event.id}")
    verdict = policy.evaluate_intent(intent, grant=grant, settings=settings, agent=agent)
    if not verdict.allowed:
        return f"policy_{_ev(verdict.decision)}: {verdict.reason}"[:200], None
    if agent.id == cand.builder_agent_id:
        return "promotion independence: the Builder cannot request its own candidate", None
    try:
        promo, _created = request_promotion(db, settings=settings, candidate=cand, agent=agent, run=run, causation=event, source_run_id=run.id)
    except ValueError as exc:
        return f"refused: {exc}"[:200], None
    promo.evidence = {**(promo.evidence or {}), "runtime_fallback": True, "reason": reason, "reoffer_event_id": str(event.id)}
    return "requested", promo.id


def settle_reoffers(db: Session, settings: SocietySettings, now: Optional[datetime] = None, *, lookback: timedelta = timedelta(hours=48)) -> int:
    """Each re-offer (:func:`retry_deferred`) whose story ended gets ONE ``promotion.reoffer_settled``
    record: why no request came, and -- when nobody said no, the fallback is on and there is
    capacity -- the runtime's own request (:func:`_fallback`). Commits when it settled one."""
    from ..models import AgentRun  # noqa: PLC0415

    now = now or utcnow()
    n = 0
    reoffers = (db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.CODE_CANDIDATE_READY, SocietyEvent.payload.has_key("promotion_retry"),
                                               SocietyEvent.created_at >= now - lookback, SocietyEvent.status != "pending")
                .order_by(SocietyEvent.created_at).all())
    for ev in reoffers:
        key = f"{SETTLE_PREFIX}{ev.id}"
        if db.query(SocietyEvent.id).filter(SocietyEvent.idempotency_key == key).first() is not None:
            continue
        if db.query(AgentRun.id).filter(AgentRun.event_id == ev.id, AgentRun.status.in_(_OPEN_RUN)).first() is not None:
            continue  # the story is still running
        cand = db.query(CodeCandidate).filter(CodeCandidate.id == ev.subject_id).first()
        if cand is None:
            continue
        fallback, promo_id = "none", None
        if _ev(cand.status) != CodeCandidateStatus.READY.value:
            reason, detail = f"candidate_{_ev(cand.status)}", ""
        elif active_promotion_for(db, cand.id) is not None:
            reason, detail = "requested", ""
        else:
            reason, detail, run = _why_not(db, ev, cand)
            if reason in FALLBACK_REASONS:
                fallback, promo_id = _fallback(db, settings, cand, run, ev, reason, now)
        emit_event(db, event_type=EventType.READY_REOFFER_SETTLED, actor_type="system", subject_type="code_candidate", subject_id=cand.id, causation=ev,
                   payload={"candidate_id": str(cand.id), "reoffer_event_id": str(ev.id), "reason": reason, "detail": detail, "fallback": fallback,
                            "promotion_id": str(promo_id) if promo_id else None},
                   idempotency_key=key)
        logger.info("promotion re-offer settled: candidate %s %s (fallback %s)", str(cand.id)[:8], reason, fallback)
        n += 1
    if n:
        db.commit()
    return n


def capacity_token(db: Session, settings: SocietySettings, now: Optional[datetime] = None) -> str:
    """Changes exactly when promotion capacity can have appeared: a cap raised, a promotion ended (a PR
    closed, merged or failed: ``ended`` counts them), or a new UTC day (the per-day cap resets)."""
    now = now or utcnow()
    ended = int(db.query(CodePromotion).filter(CodePromotion.status.notin_(ACTIVE_STATUSES)).count())
    return f"{now.date().isoformat()}/d{settings.max_promotions_per_day}/o{settings.max_open_autonomous_prs}/e{ended}"


def retry_deferred(db: Session, settings: SocietySettings, now: Optional[datetime] = None) -> int:
    """Re-offer the oldest waiting READY candidates to the Governor while capacity lasts -- once per
    candidate per :func:`capacity_token`, i.e. as soon as capacity can have appeared (owner 2026-10-29:
    not once per UTC day). A wake younger than RETRY_AFTER is still the Governor's unless capacity
    changed since it was sent. Commits when it emitted."""
    now = now or utcnow()
    cap = capacity(db, settings, now)
    slots = min(cap["promotions_left_today"], cap["open_pr_slots"])
    token = capacity_token(db, settings, now)
    sent = 0
    for item in ready_backlog(db, settings, now):
        if sent >= slots:
            break
        if item["reason"] not in ("awaiting_request", "refused"):
            continue
        last = _last_wake(db, item["candidate_id"])
        if last is not None and last.created_at > now - RETRY_AFTER and (last.payload or {}).get("capacity_token") in (None, token):
            continue  # the Governor still has its wake, and capacity did not change since
        payload = {k: v for k, v in dict((last.payload if last is not None else None) or {}).items()
                   if k not in ("redelivered_from", "promotion_retry", "capacity_token")}
        payload.setdefault("candidate_id", item["candidate_id"])
        new = emit_event(db, event_type=EventType.CODE_CANDIDATE_READY, payload={**payload, "promotion_retry": item["reason"], "capacity_token": token},
                         actor_type="system", subject_type="code_candidate", subject_id=uuid.UUID(item["candidate_id"]),
                         idempotency_key=f"{KEY_PREFIX}{item['candidate_id']}:{token}")
        if getattr(new, "deduplicated", False):
            continue
        sent += 1
        logger.info("promotion retry: candidate %s re-offered to the Governor (%s, capacity %s)", item["candidate"], item["reason"], token)
    if sent:
        db.commit()
    return sent


__all__ = ["capacity", "capacity_token", "caps", "ready_backlog", "ready_view", "retry_deferred", "settle_ended", "settle_reoffers", "worker_caps", "worker_view_caps"]
