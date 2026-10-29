"""The company's throughput funnel (owner 2026-10-29, "full throttle" experiment): deterministic, no model.

For a window (default 24 h): model spend and runs by role, tickets created/approved, design reviews
go/returned, builds attempted/delivered, READY, PRs opened, CI green, merged, rejections by reason,
$/READY and $/PR -- and, right now, the stage where work waits longest (the bottleneck). The same
numbers in 2-hour buckets. Served by ``GET /v1/society/company/funnel`` (operator), shown on /owner and
in the daily report, logged by the worker every 2 h (``society.funnel``) and checked by the validator
step ``funnel:<hours>``. Counts come from durable rows and events; structural numbers only.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import rejections
from .events import EventType, utcnow

logger = logging.getLogger("agentnet.society.funnel")

#: a candidate past these has a committed head: the build delivered (daily_report.DELIVERED)
DELIVERED = ("built", "qa_running", "qa_passed", "qa_failed", "security_review", "ready", "merged")
BUCKET = timedelta(hours=2)
#: where work can wait, in pipeline order: (stage, SQL returning the waiting rows' entry times)
STAGES = (
    ("ticket_proposed", "SELECT updated_at FROM society_tickets WHERE status = 'proposed'"),
    ("plan_awaiting_owner", "SELECT created_at FROM society_daily_plans WHERE status = 'awaiting_owner'"),
    ("approved_awaiting_design", "SELECT t.updated_at FROM society_tickets t WHERE t.status = 'approved' AND NOT EXISTS ("
                                 "SELECT 1 FROM society_design_reviews r WHERE r.ticket_id = t.id AND r.status IN ('proposing', 'reviewing', 'deciding'))"),
    ("design_review", "SELECT stage_at FROM society_design_reviews WHERE status IN ('proposing', 'reviewing', 'deciding')"),
    ("build", "SELECT updated_at FROM code_candidates WHERE status IN ('requested', 'building', 'built')"),
    ("qa_security", "SELECT updated_at FROM code_candidates WHERE status IN ('qa_running', 'qa_passed', 'qa_failed', 'security_review')"),
    ("ready_awaiting_pr", "SELECT c.updated_at FROM code_candidates c WHERE c.status = 'ready' AND NOT EXISTS ("
                          "SELECT 1 FROM code_promotions p WHERE p.candidate_id = c.id AND p.status NOT IN ('merged', 'rejected', 'superseded', 'ci_failed'))"),
    ("pr_ci", "SELECT updated_at FROM code_promotions WHERE status IN ('requested', 'validating', 'branch_ready', 'pr_open', 'ci_pending')"),
    ("pr_awaiting_merge", "SELECT updated_at FROM code_promotions WHERE status IN ('ci_passed', 'awaiting_approval', 'merge_eligible')"),
)


def _count(db: Session, sql: str, **params: Any) -> int:
    return int(db.execute(text(sql), params).scalar() or 0)


def _events(db: Session, event_type: str, t0: datetime, t1: datetime, *, fresh_only: bool = False) -> int:
    """Distinct subjects of an event type in [t0, t1); ``fresh_only`` drops re-offers and re-deliveries."""
    extra = " AND NOT (payload ? 'promotion_retry') AND NOT (payload ? 'redelivered_from')" if fresh_only else ""
    return _count(db, "SELECT COUNT(DISTINCT COALESCE(subject_id, id)) FROM society_events WHERE event_type = :e AND created_at >= :a "
                      f"AND created_at < :b{extra}", e=event_type, a=t0, b=t1)  # noqa: S608 -- a fixed clause


def counts(db: Session, t0: datetime, t1: datetime) -> Dict[str, Any]:
    """The funnel's numbers for [t0, t1)."""
    spend = float(db.execute(text("SELECT COALESCE(SUM(cost_usd), 0) FROM agent_runs WHERE created_at >= :a AND created_at < :b"), {"a": t0, "b": t1}).scalar() or 0)
    runs = {str(r[0]): int(r[1]) for r in db.execute(text(
        "SELECT COALESCE(role, '?'), COUNT(*) FROM agent_runs WHERE created_at >= :a AND created_at < :b GROUP BY 1 ORDER BY 2 DESC"), {"a": t0, "b": t1}).all()}
    reviews = {str(r[0]): int(r[1]) for r in db.execute(text(
        "SELECT status, COUNT(*) FROM society_design_reviews WHERE status IN ('go', 'returned', 'dropped') AND updated_at >= :a AND updated_at < :b GROUP BY 1"),
        {"a": t0, "b": t1}).all()}
    ready = _events(db, EventType.CODE_CANDIDATE_READY, t0, t1, fresh_only=True)
    prs = _events(db, EventType.PROMOTION_PR_OPEN, t0, t1)
    return {
        "spend_usd": round(spend, 4),
        "runs_by_role": runs,
        "tickets_created": _count(db, "SELECT COUNT(*) FROM society_tickets WHERE created_at >= :a AND created_at < :b", a=t0, b=t1),
        "tickets_approved": _events(db, EventType.COMPANY_TICKET_APPROVED, t0, t1),
        "designs_go": reviews.get("go", 0),
        "designs_returned": reviews.get("returned", 0) + reviews.get("dropped", 0),
        "builds_attempted": _count(db, "SELECT COUNT(*) FROM agent_intents WHERE intent_type = 'BUILD_CODE_CANDIDATE' AND created_at >= :a AND created_at < :b", a=t0, b=t1),
        "builds_delivered": _events(db, EventType.CODE_CANDIDATE_BUILT, t0, t1),
        "ready": ready,
        "prs_opened": prs,
        "ci_green": _events(db, EventType.PROMOTION_CI_PASSED, t0, t1),
        "merged": _events(db, EventType.PROMOTION_MERGED, t0, t1),
        "usd_per_ready": round(spend / ready, 2) if ready else None,
        "usd_per_pr": round(spend / prs, 2) if prs else None,
    }


def stages(db: Session, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Each stage's waiting work now: count, oldest and median wait (hours)."""
    now = now or utcnow()
    out = []
    for name, sql in STAGES:
        ages = sorted(max(0.0, (now - r[0]).total_seconds() / 3600) for r in db.execute(text(sql)).all() if r[0] is not None)
        out.append({"stage": name, "waiting": len(ages), "oldest_h": round(ages[-1], 1) if ages else None,
                    "median_h": round(statistics.median(ages), 1) if ages else None})
    return out


def bottleneck(stage_rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The stage where work waits longest now: the highest median wait among stages holding work."""
    busy = [s for s in stage_rows if s["waiting"]]
    return max(busy, key=lambda s: (s["median_h"], s["waiting"])) if busy else None


def funnel(db: Session, now: Optional[datetime] = None, hours: int = 24) -> Dict[str, Any]:
    now = now or utcnow()
    t0 = now - timedelta(hours=hours)
    buckets = []
    start = t0
    while start < now:
        end = min(now, start + BUCKET)
        c = counts(db, start, end)
        buckets.append({"from": start.isoformat(), "to": end.isoformat(),
                        **{k: c[k] for k in ("spend_usd", "tickets_created", "tickets_approved", "designs_go", "builds_delivered", "ready", "prs_opened", "merged")}})
        start = end
    st = stages(db, now)
    return {"window_hours": hours, "as_of": now.isoformat(), **counts(db, t0, now),
            "rejections": rejections.breakdown(db, since=t0)["by_reason"], "stages": st, "bottleneck": bottleneck(st), "by_2h": buckets}


def summary(f: Dict[str, Any]) -> str:
    """One line for the report and the log."""
    b = f.get("bottleneck") or {}
    return (f"Funnel {f['window_hours']}h: ${f['spend_usd']:.2f}; tickets {f['tickets_created']} created / {f['tickets_approved']} approved; "
            f"designs {f['designs_go']} go / {f['designs_returned']} returned; builds {f['builds_delivered']}/{f['builds_attempted']}; "
            f"READY {f['ready']}; PRs {f['prs_opened']} (CI green {f['ci_green']}, merged {f['merged']}); "
            f"$/READY {f['usd_per_ready'] if f['usd_per_ready'] is not None else 'n/a'}, $/PR {f['usd_per_pr'] if f['usd_per_pr'] is not None else 'n/a'}; "
            f"bottleneck {b.get('stage', 'none')}" + (f" ({b['waiting']} waiting, median {b['median_h']}h)" if b else ""))


def log_snapshot(db: Session, now: Optional[datetime] = None, hours: int = 2) -> Dict[str, Any]:
    """The worker's 2-hourly line (structural numbers only), so a run can be followed from the logs."""
    f = funnel(db, now, hours=hours)
    logger.info("society.funnel %s", summary(f))
    return f


__all__ = ["BUCKET", "STAGES", "bottleneck", "counts", "funnel", "log_snapshot", "stages", "summary"]
