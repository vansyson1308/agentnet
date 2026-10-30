"""Heartbeat precheck: a role is woken by the hourly heartbeat only when it has something to act on
(deterministic, no model).

Staging 2026-10-25: ~$3.5/day went to hourly ``society.heartbeat`` runs of the Builder, Scout and
Governor (~$0.05 each) that changed nothing. With ``SOCIETY_HEARTBEAT_PRECHECK_ENABLED`` the worker
asks ``heartbeat_work`` first; with nothing to do the run is SKIPPED as ``idle: ...`` -- no context,
no model call. Every other wake (a ticket approved, a review question, a candidate event) is
untouched: those are targeted events, never heartbeats.

What counts as work, per role:

* Builder -- a candidate in a Builder stage (requested, building, qa_failed): the heartbeat exists
  to recover a lost one-shot wake (roles.py).
* Governor -- an unreviewed improvement proposal, or a READY candidate with no promotion yet.
* Architect -- an approved ticket with no candidate and no open design review: work waiting for a
  design (owner 2026-10-29 G1v: never idle while work exists; tickets.wake_undesigned re-sends its wake).
* every role -- a backlog item published since its last completed run.
* liveness -- no completed run for ``SOCIETY_HEARTBEAT_MAX_IDLE_HOURS``: the role is woken anyway,
  so a precheck gap can delay work but never strand it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

IDLE_PREFIX = "idle: "
BUILDER_STAGES = ("requested", "building", "qa_failed")


def _last_completed(db: Session, agent_id: Any) -> Optional[datetime]:
    return db.execute(text("SELECT MAX(completed_at) FROM agent_runs WHERE agent_id = :a AND status = 'completed'"), {"a": agent_id}).scalar()


def heartbeat_work(db: Session, *, agent_id: Any, role: str, now: datetime, max_idle_hours: int) -> Tuple[bool, str]:
    """(has work, why). Never raises for an unknown role: the shared checks still apply."""
    last = _last_completed(db, agent_id)
    if last is None or last < now - timedelta(hours=max_idle_hours):
        return True, f"liveness: no completed run in {max_idle_hours}h"
    if role == "builder":
        n = db.execute(text("SELECT COUNT(*) FROM code_candidates WHERE status IN ('requested', 'building', 'qa_failed')")).scalar() or 0
        if n:
            return True, f"{n} candidate(s) in a Builder stage"
    if role == "governor":
        n = db.execute(text("SELECT COUNT(*) FROM improvement_proposals WHERE status = 'PROPOSED'")).scalar() or 0
        if n:
            return True, f"{n} unreviewed proposal(s)"
        n = db.execute(text("SELECT COUNT(*) FROM code_candidates c WHERE c.status = 'ready' "
                            "AND NOT EXISTS (SELECT 1 FROM code_promotions p WHERE p.candidate_id = c.id)")).scalar() or 0
        if n:
            return True, f"{n} READY candidate(s) without a promotion"
    if role == "architect":
        n = db.execute(text("SELECT COUNT(*) FROM society_tickets t WHERE t.status = 'approved' AND t.candidate_id IS NULL AND NOT EXISTS ("
                            "SELECT 1 FROM society_design_reviews r WHERE r.ticket_id = t.id AND r.status IN ('proposing', 'reviewing', 'deciding'))")).scalar() or 0
        if n:
            return True, f"{n} approved ticket(s) without a design"
    n = db.execute(text("SELECT COUNT(*) FROM society_events WHERE event_type = 'backlog.item' AND created_at > :t"), {"t": last}).scalar() or 0
    if n:
        return True, f"{n} new backlog item(s) since the last run"
    return False, "nothing to act on (no candidate in its stage, no open proposal, no ticket to design, no new backlog item)"


def savings(db: Session, now: datetime) -> Dict[str, Any]:
    """Idle heartbeat wakes skipped in 24h and their estimated model cost (the mean cost of a
    heartbeat run that did call the model over the last 7 days)."""
    idle = db.execute(text("SELECT COUNT(*) FROM agent_runs WHERE status = 'skipped' AND error LIKE :p AND completed_at >= :t"),
                      {"p": IDLE_PREFIX + "%", "t": now - timedelta(hours=24)}).scalar() or 0
    mean = db.execute(text("SELECT AVG(r.cost_usd) FROM agent_runs r JOIN society_events e ON e.id = r.event_id "
                           "WHERE e.event_type = 'society.heartbeat' AND r.status = 'completed' AND r.completed_at >= :t"),
                      {"t": now - timedelta(days=7)}).scalar()
    mean = float(mean or 0)
    return {"idle_skips_24h": int(idle), "mean_heartbeat_run_usd_7d": round(mean, 4), "saved_usd_24h_est": round(idle * mean, 4)}
