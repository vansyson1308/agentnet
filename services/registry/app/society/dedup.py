"""In-flight dedup: one piece of work per product file and objective (G1y item 3, 2026-11-01).

Staging 2026-10-31: #136 and #143 added the same 'List your agent' CTA to landing.html, #150 and
#151 are the same marketplace.html section, #141 and #145 document the same SDK methods -- each
pair conflicts on merge and only one can ever land.

* ``conflicts`` -- the design gate (executor ``PROPOSE_DESIGN``) refuses a design whose PRODUCT
  file (tests excluded) is already targeted, for the same objective, by another open ticket's
  approved design, a live candidate (building, READY) or an open PR. Structural reason only.
* ``lever_taken`` -- the work generator does not file a ticket for a file that is already in flight
  for that objective.
* ``supersede_duplicates`` -- when two already exist, one is superseded -- the LATER, unless only the
  later one can be merged by the controller itself (``_keep_rank``): candidate
  REJECTED with a structural reason, its open PR closed through the promotion provider (its own
  bot PR), its ticket closed, and ITS OWN implementation task refunded once through
  ``task_service.fail_task_with_refund`` (the ordinary escrow path, run last). Nothing here touches a
  wallet; nothing widens a spec or decides a merge.

Deterministic, no model, no new authority: it only ends work, it never starts or approves any.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Set

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import CodeCandidate, CodeCandidateStatus, CodePromotion, PromotionStatus, TaskSession, TaskStatus
from .events import EventType, emit_event

logger = logging.getLogger("agentnet.society.dedup")

_TEST_RE = re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test)\.py$|^tests/")
TERMINAL_CANDIDATE = ("merged", "rejected", "failed", "abandoned")
REASON = "superseded_duplicate"


def product_files(paths: Iterable[Any]) -> Set[str]:
    """The files a change ships (its tests excluded: two candidates may each carry their own test)."""
    return {str(p) for p in paths or [] if p and not _TEST_RE.search(str(p))}


def _ev(v: Any) -> str:
    return v.value if hasattr(v, "value") else str(v)


def in_flight(db: Session, objective_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Live work with its objective and product files, oldest first: candidates not yet ended
    (their spec + changed files) and approved designs of open tickets that have no live candidate."""
    rows = db.execute(text(
        "SELECT c.id, c.created_at, c.spec -> 'files_allowed' AS allowed, c.changed_files, c.status::text AS status, t.id AS ticket_id, t.objective_id, "
        "       (SELECT p.external_pr_number FROM code_promotions p WHERE p.candidate_id = c.id AND p.status::text IN "
        "        ('requested','validating','branch_ready','pr_open','ci_pending','ci_passed','ci_failed','awaiting_approval','merge_eligible') "
        "        ORDER BY p.created_at DESC LIMIT 1) AS pr, "
        "       (SELECT p.eligibility -> 'owner_merge_reasons' FROM code_promotions p WHERE p.candidate_id = c.id ORDER BY p.created_at DESC LIMIT 1) AS owner_reasons "
        "FROM code_candidates c JOIN society_tickets t ON t.proposal_id = c.proposal_id "
        "WHERE c.status::text NOT IN ('merged','rejected','failed','abandoned') AND (CAST(:o AS VARCHAR) IS NULL OR t.objective_id = :o)"),
        {"o": objective_id}).mappings().all()
    out = [{"kind": "candidate", "id": r["id"], "ticket_id": r["ticket_id"], "objective_id": r["objective_id"], "created_at": r["created_at"],
            "status": r["status"], "pr": r["pr"], "self_mergeable": r["pr"] is not None and r["owner_reasons"] == [], "files": product_files(list(r["allowed"] or []) + list(r["changed_files"] or []))} for r in rows]
    busy = {str(x["ticket_id"]) for x in out}
    designs = db.execute(text(
        "SELECT DISTINCT ON (r.ticket_id) r.ticket_id, r.proposal, r.created_at, t.objective_id, t.files FROM society_design_reviews r "
        "JOIN society_tickets t ON t.id = r.ticket_id WHERE r.status = 'go' AND t.status IN ('approved', 'building', 'ready') "
        "AND (CAST(:o AS VARCHAR) IS NULL OR t.objective_id = :o) ORDER BY r.ticket_id, r.created_at DESC"), {"o": objective_id}).mappings().all()
    for d in designs:
        if str(d["ticket_id"]) in busy:
            continue
        files = list((d["proposal"] or {}).get("files") or []) + list(d["files"] or [])
        out.append({"kind": "design", "id": d["ticket_id"], "ticket_id": d["ticket_id"], "objective_id": d["objective_id"], "created_at": d["created_at"],
                    "status": "approved", "pr": None, "files": product_files(files)})
    return sorted(out, key=lambda x: x["created_at"])


def _label(item: Dict[str, Any]) -> str:
    return f"{item['kind']} {str(item['id'])[:8]}" + (f" (PR #{item['pr']})" if item.get("pr") else "")


def conflicts(db: Session, files: Iterable[str], objective_id: Optional[str], ticket_id: Any = None) -> List[str]:
    """Why a design for ``ticket_id`` duplicates live work (one line per file; [] = it does not)."""
    if not objective_id:
        return []
    mine = product_files(files)
    problems = []
    for item in in_flight(db, objective_id):
        if ticket_id is not None and str(item["ticket_id"]) == str(ticket_id):
            continue
        for path in sorted(mine & item["files"]):
            problems.append(f"duplicate: {path} is already targeted for {objective_id} by {_label(item)} -- choose a file no open work targets")
    return problems


def lever_taken(db: Session, levers: Iterable[Optional[str]], objective_id: Optional[str]) -> bool:
    wanted = product_files(lv for lv in levers if lv)
    return bool(wanted) and any(wanted & item["files"] for item in in_flight(db, objective_id))


def _keep_rank(item: Dict[str, Any]) -> tuple:
    """Which of two duplicates survives: one the controller may merge itself, then one with an open
    PR, then the earlier. Keeping a RED twin and closing its self-mergeable copy would trade work
    that can land for work that waits on the owner."""
    return (0 if item.get("self_mergeable") else 1 if item.get("pr") else 2, item["created_at"])


def duplicate_pairs(db: Session) -> List[tuple]:
    """(kept, superseded, shared files): live CANDIDATES that target the same product file for the
    same objective; the one ``_keep_rank`` puts first stays, the others are superseded."""
    live = sorted((x for x in in_flight(db) if x["kind"] == "candidate" and x["objective_id"]), key=_keep_rank)
    pairs, taken = [], set()
    for i, later in enumerate(live):
        for earlier in live[:i]:
            if str(earlier["id"]) in taken or earlier["objective_id"] != later["objective_id"]:
                continue
            shared = sorted(earlier["files"] & later["files"])
            if shared:
                pairs.append((earlier, later, shared))
                taken.add(str(later["id"]))
                break
    return pairs


def supersede_duplicates(db: Session, provider: Any = None, *, limit: int = 10) -> int:
    """End the LATER of each duplicate pair (see module doc). One commit per superseded candidate."""
    from . import tickets  # noqa: PLC0415
    from .. import task_service  # noqa: PLC0415

    done = 0
    for earlier, later, shared in duplicate_pairs(db)[:limit]:
        cand = db.query(CodeCandidate).filter(CodeCandidate.id == later["id"]).with_for_update().first()
        if cand is None or _ev(cand.status) in TERMINAL_CANDIDATE:
            continue
        reason = f"{REASON}: {', '.join(shared[:3])} for {later['objective_id']} is already in {_label(earlier)}"[:500]
        task_to_fail = None
        if cand.task_id is not None:
            task = db.query(TaskSession).filter(TaskSession.id == cand.task_id).with_for_update().first()
            if task is not None and _ev(task.status) in (TaskStatus.INITIATED.value, TaskStatus.IN_PROGRESS.value):
                task_to_fail = task
        previous = _ev(cand.status)
        cand.status = CodeCandidateStatus.REJECTED
        cand.error = reason
        closed_pr = None
        for promo in db.query(CodePromotion).filter(CodePromotion.candidate_id == cand.id).all():
            if _ev(promo.status) in ("merged", "rejected"):
                continue
            if promo.external_pr_number and provider is not None:
                try:
                    provider.close_pr(promo, f"Closed by the AgentNet promotion controller: {reason}")
                    closed_pr = promo.external_pr_number
                except Exception as exc:  # noqa: BLE001 -- the PR is still ended in our records; the owner may close it by hand
                    logger.warning("dedup: could not close PR #%s: %s", promo.external_pr_number, type(exc).__name__)
            promo.status = PromotionStatus.REJECTED
            promo.failure_reason = reason
            promo.worker_id = None
            promo.lease_expires_at = None
        if later["ticket_id"] is not None:
            tickets.set_status(db, later["ticket_id"], "closed", reason[:300])
        db.flush()
        emit_event(db, event_type=EventType.CODE_CANDIDATE_REJECTED,
                   payload={"candidate_id": str(cand.id), "title": cand.title, "previous_status": previous, "reason_code": REASON,
                            "superseded_by": str(earlier["id"]), "shared_files": shared[:5], "objective_id": later["objective_id"],
                            "closed_pr": closed_pr, "task_refunded": task_to_fail is not None},
                   actor_type="system", subject_type="code_candidate", subject_id=cand.id, correlation_id=cand.correlation_id,
                   idempotency_key=f"dedup:{cand.id}:superseded", notify=True)
        if task_to_fail is not None:  # last: the escrow path commits the rejection, its event and the one refund together
            task_service.fail_task_with_refund(db=db, task_id=task_to_fail.id, error_message=reason[:500])
        else:
            db.commit()
        done += 1
        logger.info("dedup: candidate %s superseded by %s (%s)", str(cand.id)[:8], str(earlier["id"])[:8], ", ".join(shared[:3]))
    return done


__all__ = ["conflicts", "duplicate_pairs", "in_flight", "lever_taken", "product_files", "supersede_duplicates", "REASON"]
