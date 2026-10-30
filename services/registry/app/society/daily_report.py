"""The owner's daily report (deterministic, no model): one plain-text page from the database.

What was built, model $ by role, builds delivered vs attempted, design reviews held, revenue and the
key results against the north star's pace, and the owner actions waiting. Served as
``GET /v1/society/company/report`` and as the plans' ``summary_text``; structural numbers and our own
labels only (ticket titles are system-written).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from . import funnel, goals, owner_actions, promotion_queue, rejections
from .config import get_settings
from .events import utcnow

#: a candidate past these has a committed head: the build delivered
DELIVERED = ("built", "qa_running", "qa_passed", "qa_failed", "security_review", "ready", "merged")


def build(db: Session, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or utcnow()
    t = now - timedelta(hours=24)
    by_role = {r: round(float(c), 4) for r, c in db.execute(text(
        "SELECT COALESCE(role, '?'), SUM(cost_usd) FROM agent_runs WHERE created_at >= :t GROUP BY 1 ORDER BY 2 DESC"), {"t": t}).all()}
    attempted = int(db.execute(text("SELECT COUNT(*) FROM agent_intents WHERE intent_type = 'BUILD_CODE_CANDIDATE' AND created_at >= :t"), {"t": t}).scalar() or 0)
    delivered = int(db.execute(text("SELECT COUNT(*) FROM code_candidates WHERE status IN :s AND updated_at >= :t").bindparams(
        bindparam("s", expanding=True)), {"s": list(DELIVERED), "t": t}).scalar() or 0)
    ready = [r[0] for r in db.execute(text("SELECT title FROM code_candidates WHERE status = 'ready' AND updated_at >= :t ORDER BY updated_at DESC LIMIT 5"), {"t": t}).all()]
    reviews = dict(db.execute(text("SELECT status, COUNT(*) FROM society_design_reviews WHERE updated_at >= :t GROUP BY 1"), {"t": t}).all())
    cycles = int(db.execute(text("SELECT COUNT(*) FROM society_company_cycles WHERE created_at >= :t"), {"t": t}).scalar() or 0)
    card = goals.scorecard(db, now)
    tin, read, usd, n = db.execute(text(
        "SELECT COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_cached), 0), COALESCE(SUM(cost_usd), 0), COUNT(*) FROM agent_runs r "
        "WHERE created_at >= :t AND model_provider = 'anthropic' AND cost_usd > 0 AND NOT EXISTS "
        "(SELECT 1 FROM agent_intents i WHERE i.run_id = r.id AND i.intent_type = 'BUILD_CODE_CANDIDATE')"), {"t": t}).one()
    cache = {"runs": int(n), "input": int(tin), "cache_read": int(read), "hit_rate": round(int(read) / int(tin), 4) if tin else None,
             "usd_per_run": round(float(usd) / int(n), 4) if n else None}
    data = {"window_hours": 24, "cycles": cycles, "spend_usd_by_role": by_role, "spend_usd_total": round(sum(by_role.values()), 4),
            "builds": {"attempted": attempted, "delivered": delivered}, "ready": ready, "design_reviews": {k: int(v) for k, v in reviews.items()},
            "north_star": card.get("north_star"), "owner_actions_open": owner_actions.open_count(db),
            "ready_without_pr": [{k: b[k] for k in ("candidate", "reason", "requests")} for b in promotion_queue.ready_backlog(db, get_settings(), now, from_worker=True)],
            "change_budget": promotion_queue.worker_view_caps(db, get_settings(), now), "rejections": rejections.breakdown(db),
            "ready_candidates": promotion_queue.ready_view(db, get_settings(), now), "funnel": funnel.funnel(db, now, hours=24, daily_budget_usd=funnel.worker_budget(db, float(get_settings().daily_model_budget_usd)))}
    return {"data": data, "text": render(data, card, owner_actions.summary_lines(db))}


def cache_line(c: Dict[str, Any]) -> str:
    """Decide runs only (a build is a multi-turn loop, cached on its own)."""
    if not c.get("runs") or not c.get("input"):
        return "Prompt cache (24h, decide runs): no priced runs"
    return (f"Prompt cache (24h, decide runs): {c['hit_rate'] * 100:.1f}% of {c['input']:,} input tokens read from cache; "
            f"${c['usd_per_run']:.3f} per run")


def render(d: Dict[str, Any], card: Dict[str, Any], actions: list) -> str:
    roles = ", ".join(f"{r} ${c:.2f}" for r, c in d["spend_usd_by_role"].items()) or "none"
    reviews = ", ".join(f"{k} {v}" for k, v in sorted(d["design_reviews"].items())) or "none"
    waiting = ", ".join(f"{b['candidate']} ({b['reason']})" for b in d.get("ready_without_pr") or []) or "none"
    cb = d.get("change_budget") or {}
    lines = [f"Last 24h: {d['cycles']} cycle(s); model spend ${d['spend_usd_total']:.2f} ({roles})",
             cache_line(d.get("prompt_cache") or {}),
             f"Builds: {d['builds']['delivered']} delivered / {d['builds']['attempted']} attempted; READY: {', '.join(d['ready']) or 'none'}",
             f"READY without a PR: {waiting}",
             f"Change budget: {cb.get('promotions_today')}/{cb.get('max_promotions_per_day')} promotions today, {cb.get('open_prs')}/{cb.get('max_open_autonomous_prs')} autonomous PRs open"
             f" (provider {cb.get('provider')}, auto-merge {'on' if cb.get('auto_merge_enabled') else 'off'})",
             rejections.summary(d["rejections"]) if d.get("rejections") else "Rejected: n/a",
             f"Design reviews: {reviews}",
             funnel.summary(d["funnel"]) if d.get("funnel") else "Funnel: n/a",
             goals.summary(card) if card.get("north_star") else "no north star in the charter",
             f"Owner actions waiting: {d['owner_actions_open']}"] + actions
    return "\n".join(lines)
