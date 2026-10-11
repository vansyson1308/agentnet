"""Deterministic candidate health: stale QA verdicts, stalled and obsolete work.

Staging/production, live since 2026-09-29 (found 2026-10-09): a candidate the
Builder re-submitted after a QA failure went back to BUILT, but its
``qa_report`` still said ``verdict=fail`` for the previous head. Every role
read that as "failed": nobody issued REQUEST_QA, and the Builder may neither
re-submit nor DECLINE a BUILT candidate. The re-submission's own wake had been
spent in a long story. 18 hours, 57 runs, 100% WRITE_MEMORY.

Three remedies, none of them a model decision:

* **a verdict belongs to a head.** Re-submitting clears the old verdict and
  summary (the attempt count and a ``previous`` record stay, so the QA attempt
  cap still holds), and a QA request for the new head is emitted as a fresh
  system story (depth 0, its own correlation) that the loop breaker of the
  original story cannot swallow -- one per head (idempotency key).
  :func:`heal_stale_verdicts` applies the same to rows already in that state.
* **stalled** -- an open candidate with no lifecycle progress (a status-changing
  lifecycle event) for ``SOCIETY_CANDIDATE_STALL_HOURS`` is surfaced to the
  operator queue and no longer holds a portfolio slot
  (``company._classify``). It is NOT abandoned: only an operator abandons
  (``candidate_admin``).
* **obsolete** -- an open candidate whose story's public-surface anomaly has
  RECOVERED is marked obsolete for the operator. Status is untouched.

Operator notices are durable ``code_candidate.stalled`` / ``code_candidate.obsolete``
events (system, no role subscribes, emitted once per state) plus the
``candidates`` section of ``GET /v1/society/approvals``. Payloads are
structural: ids, statuses, timestamps, counts.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import CodeCandidate, CodeCandidateStatus, CodePromotion, SocietyEvent
from .config import SocietySettings
from .events import EventType, emit_event, utcnow

logger = logging.getLogger("agentnet.society.candidate_health")

#: Candidates a role still has to move. READY waits for a person (promotion).
OPEN_STATUSES = (
    CodeCandidateStatus.REQUESTED,
    CodeCandidateStatus.BUILDING,
    CodeCandidateStatus.BUILT,
    CodeCandidateStatus.QA_RUNNING,
    CodeCandidateStatus.QA_FAILED,
    CodeCandidateStatus.SECURITY_REVIEW,
)
#: Events that record a lifecycle transition of the candidate they are about.
LIFECYCLE_EVENTS = (
    EventType.CODE_CHANGE_REQUESTED,
    EventType.CODE_CANDIDATE_BUILT,
    EventType.CODE_CANDIDATE_QA_PASSED,
    EventType.CODE_CANDIDATE_QA_FAILED,
    EventType.CODE_CANDIDATE_SECURITY_REVIEW,
    EventType.CODE_CANDIDATE_READY,
    EventType.CODE_CANDIDATE_REJECTED,
)
#: Payload markers of a lifecycle event that only re-wakes a role: no progress.
WAKE_ONLY_MARKERS = ("redelivered_from", "requeued", "qa_request")
QA_REQUEST_KEY = "qa-request:"
MAX_NOTICES_PER_SWEEP = 20


def _ev(v: Any) -> str:
    return str(getattr(v, "value", v))


# ── a) a verdict belongs to the head it judged ─────────────────────────────


def reset_qa_report(qa: Optional[Dict[str, Any]], *, new_head: Optional[str]) -> Dict[str, Any]:
    """The QA report of a re-submitted candidate: no verdict, no summary, no
    failures for the new head; the attempt count (QA attempt cap) and a short
    ``previous`` record (for the operator) are kept."""
    qa = dict(qa or {})
    previous = {k: qa.get(k) for k in ("verdict", "head_sha", "attempts") if qa.get(k) is not None}
    if qa.get("failures"):
        previous["failures"] = list(qa.get("failures") or [])[:5]
    out: Dict[str, Any] = {"attempts": int(qa.get("attempts") or 0), "awaiting_head": new_head}
    if previous:
        out["previous"] = previous
    return out


def has_stale_verdict(cand: CodeCandidate) -> bool:
    """A BUILT candidate whose verdict was given for another (or unknown) head."""
    qa = cand.qa_report or {}
    return _ev(cand.status) == CodeCandidateStatus.BUILT.value and bool(qa.get("verdict")) and qa.get("head_sha") != cand.head_sha


def emit_qa_request(db: Session, cand: CodeCandidate, *, reason: str, source_intent_id: Optional[uuid.UUID] = None) -> SocietyEvent:
    """Wake QA for the candidate's current head, in a fresh story.

    The wake is the ordinary ``code_candidate.built`` (routed to QA only) with
    ``qa_request: true``. No causation and no inherited correlation: the story
    that produced the head may already be at the loop breaker's run budget.
    One request per head (idempotency key)."""
    payload = {
        "candidate_id": str(cand.id),
        "title": cand.title,
        "head_sha": cand.head_sha,
        "branch_name": cand.branch_name,
        "changed_files": list(cand.changed_files or [])[:20],
        "requires_security_review": bool(cand.requires_security_review),
        "qa_request": True,
        "reason": reason,
        "candidate_correlation_id": str(cand.correlation_id) if cand.correlation_id else None,
    }
    if source_intent_id is not None:
        payload["source_intent_id"] = str(source_intent_id)
    return emit_event(
        db,
        event_type=EventType.CODE_CANDIDATE_BUILT,
        payload=payload,
        actor_type="system",
        subject_type="code_candidate",
        subject_id=cand.id,
        idempotency_key=f"{QA_REQUEST_KEY}{cand.id}:{cand.head_sha or ''}"[:160],
    )


def heal_stale_verdicts(db: Session, *, limit: int = MAX_NOTICES_PER_SWEEP) -> int:
    """Rows already in the deadlock: BUILT with another head's verdict. Clear
    it and request QA for the current head. Flushes; returns how many."""
    rows = (
        db.query(CodeCandidate)
        .filter(CodeCandidate.status == CodeCandidateStatus.BUILT, CodeCandidate.qa_report["verdict"].isnot(None))
        .order_by(CodeCandidate.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .all()
    )
    healed = 0
    for cand in rows:
        if not has_stale_verdict(cand):
            continue
        cand.qa_report = reset_qa_report(cand.qa_report, new_head=cand.head_sha)
        emit_qa_request(db, cand, reason="stale_verdict")
        healed += 1
        logger.info("candidate %s: cleared a QA verdict of another head; QA requested for %s", cand.id, (cand.head_sha or "")[:12])
    db.flush()
    return healed


# ── b) stalled ─────────────────────────────────────────────────────────────


def _progress_events(db: Session, ids: Iterable[uuid.UUID]):
    """(candidate id, newest progress time) for these candidates."""
    ids = list(ids)
    if not ids:
        return {}
    wake_only = None
    for marker in WAKE_ONLY_MARKERS:
        clause = SocietyEvent.payload.has_key(marker)  # noqa: W601 - JSONB ? operator
        wake_only = clause if wake_only is None else (wake_only | clause)
    rows = (
        db.query(SocietyEvent.subject_id, func.max(SocietyEvent.created_at))
        .filter(
            SocietyEvent.subject_type == "code_candidate",
            SocietyEvent.subject_id.in_(ids),
            SocietyEvent.event_type.in_(list(LIFECYCLE_EVENTS)),
            ~wake_only,
        )
        .group_by(SocietyEvent.subject_id)
        .all()
    )
    return {sid: ts for sid, ts in rows}


def last_progress(db: Session, cands: List[CodeCandidate]) -> Dict[uuid.UUID, datetime]:
    """The newest lifecycle transition of each candidate (its creation if none)."""
    progress = _progress_events(db, [c.id for c in cands])
    out = {}
    for c in cands:
        ts = progress.get(c.id)
        out[c.id] = max(t for t in (ts, c.created_at) if t is not None)
    return out


def stalled_candidates(db: Session, settings: SocietySettings, now: Optional[datetime] = None, *, ids: Optional[Iterable[uuid.UUID]] = None) -> List[Dict[str, Any]]:
    """Open candidates with no lifecycle progress for the stall window."""
    now = now or utcnow()
    q = db.query(CodeCandidate).filter(CodeCandidate.status.in_(list(OPEN_STATUSES)))
    if ids is not None:
        ids = list(ids)
        if not ids:
            return []
        q = q.filter(CodeCandidate.id.in_(ids))
    cands = q.order_by(CodeCandidate.created_at).limit(500).all()
    cutoff = now - timedelta(hours=settings.candidate_stall_hours)
    progress = last_progress(db, cands)
    out = []
    for c in cands:
        since = progress[c.id]
        if since <= cutoff:
            out.append({
                "candidate_id": str(c.id),
                "title": c.title,
                "status": _ev(c.status),
                "proposal_id": str(c.proposal_id) if c.proposal_id else None,
                "last_progress_at": since.isoformat(),
                "hours_without_progress": round((now - since).total_seconds() / 3600, 1),
                "qa_attempts": int((c.qa_report or {}).get("attempts") or 0),
            })
    return out


def stalled_ids(db: Session, settings: SocietySettings, ids: Iterable[uuid.UUID], now: Optional[datetime] = None) -> Set[uuid.UUID]:
    return {uuid.UUID(r["candidate_id"]) for r in stalled_candidates(db, settings, now, ids=ids)}


# ── c) obsolete ────────────────────────────────────────────────────────────


def obsolete_candidates(db: Session) -> List[Dict[str, Any]]:
    """Open candidates (or READY ones without a promotion) whose story's
    public-surface anomaly has recovered."""
    promoted = db.query(CodePromotion.candidate_id)
    rows = (
        db.query(CodeCandidate, SocietyEvent)
        .join(SocietyEvent, SocietyEvent.correlation_id == CodeCandidate.correlation_id)
        .filter(
            SocietyEvent.event_type == EventType.PUBLIC_SURFACE_RECOVERED,
            (CodeCandidate.status.in_(list(OPEN_STATUSES)))
            | ((CodeCandidate.status == CodeCandidateStatus.READY) & CodeCandidate.id.notin_(promoted)),
        )
        .order_by(CodeCandidate.created_at)
        .limit(200)
        .all()
    )
    out: Dict[str, Dict[str, Any]] = {}
    for c, ev in rows:
        out.setdefault(str(c.id), {
            "candidate_id": str(c.id),
            "title": c.title,
            "status": _ev(c.status),
            "reason": "target_recovered",
            "recovered_event_id": str(ev.id),
            "anomaly_event_id": (ev.payload or {}).get("anomaly_event_id"),
            "recovered_at": ev.created_at.isoformat() if ev.created_at else None,
        })
    return list(out.values())


# ── operator surface ───────────────────────────────────────────────────────


#: A promotion still in flight (its PR may wait on the owner).
_PROMOTION_OPEN = ("requested", "validating", "branch_ready", "pr_open", "ci_pending", "ci_passed", "ci_failed", "awaiting_approval", "merge_eligible")


def owner_merge_queue(db: Session) -> List[Dict[str, Any]]:
    """RED candidates' open PRs: only the owner merges them. Each row carries the ticket's
    objective link and the BENCH VERDICT numbers from the persisted QA report (structural)."""
    from . import tickets  # noqa: PLC0415

    rows = (db.query(CodePromotion, CodeCandidate).join(CodeCandidate, CodeCandidate.id == CodePromotion.candidate_id)
            .filter(CodePromotion.risk_tier.in_(["red", "never"]), CodePromotion.status.in_(_PROMOTION_OPEN))
            .order_by(CodePromotion.created_at.desc()).limit(50).all())
    out = []
    for promo, cand in rows:
        t = tickets.for_proposal(db, cand.proposal_id)
        out.append({
            "candidate_id": str(cand.id), "promotion_id": str(promo.id), "title": cand.title, "risk_tier": _ev(promo.risk_tier), "status": _ev(promo.status),
            "pr_number": promo.external_pr_number, "pr_url": promo.external_pr_url, "ci_state": promo.ci_state,
            "ticket": tickets.link_text(t) if t else None,
            "bench_verdict": [{k: b.get(k) for k in ("task_id", "passed", "delivered", "baseline_delivered", "repeat", "runs", "cost_usd")}
                              for b in (cand.qa_report or {}).get("bench_proof") or [] if isinstance(b, dict)],
        })
    return out


def operator_queue(db: Session, settings: SocietySettings, now: Optional[datetime] = None) -> Dict[str, List[Dict[str, Any]]]:
    return {"stalled": stalled_candidates(db, settings, now), "obsolete": obsolete_candidates(db), "owner_merge": owner_merge_queue(db)}


def _notice(db: Session, event_type: str, row: Dict[str, Any], key: str) -> bool:
    ev = emit_event(
        db,
        event_type=event_type,
        payload=row,
        actor_type="system",
        subject_type="code_candidate",
        subject_id=uuid.UUID(row["candidate_id"]),
        idempotency_key=key[:160],
        notify=False,
    )
    return not getattr(ev, "deduplicated", False)


def sweep(db: Session, settings: SocietySettings, now: Optional[datetime] = None) -> Dict[str, int]:
    """One deterministic pass: heal stale verdicts, then record a durable
    operator notice once per stalled episode and once per recovery. Commits
    when it changed anything."""
    now = now or utcnow()
    healed = heal_stale_verdicts(db)
    stalled = obsolete = 0
    for row in stalled_candidates(db, settings, now)[:MAX_NOTICES_PER_SWEEP]:
        stalled += _notice(db, EventType.CODE_CANDIDATE_STALLED, row, f"candidate-stalled:{row['candidate_id']}:{row['last_progress_at']}")
    for row in obsolete_candidates(db)[:MAX_NOTICES_PER_SWEEP]:
        obsolete += _notice(db, EventType.CODE_CANDIDATE_OBSOLETE, row, f"candidate-obsolete:{row['candidate_id']}:{row['recovered_event_id']}")
    if healed or stalled or obsolete:
        db.commit()
    return {"healed": healed, "stalled_notices": stalled, "obsolete_notices": obsolete}


__all__ = [
    "OPEN_STATUSES",
    "LIFECYCLE_EVENTS",
    "reset_qa_report",
    "has_stale_verdict",
    "emit_qa_request",
    "heal_stale_verdicts",
    "last_progress",
    "stalled_candidates",
    "stalled_ids",
    "obsolete_candidates",
    "operator_queue",
    "owner_merge_queue",
    "sweep",
]
