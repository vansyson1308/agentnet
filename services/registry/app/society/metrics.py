"""Deterministic metric sources for the company charter's key results (no model).

A key result names a ``metric_id``; it must resolve here or the charter is
refused at load (``charter.py``). Each metric is ONE fixed, read-only SQL
aggregate over existing tables, bound only to a window start ``:t``; it
returns a number, or ``None`` while there is no data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class Metric:
    metric_id: str
    unit: str
    description: str
    window: timedelta
    sql: str


_JOURNEY = "FROM maintenance_observations WHERE sli = 'core_journey' AND observed_at >= :t"
REGISTRY: Dict[str, Metric] = {m.metric_id: m for m in (
    Metric("core_journey_success_rate", "ratio", "share of hourly core-journey probes that passed (24h)", timedelta(hours=24),
           f"SELECT AVG(CASE WHEN ok THEN 1.0 ELSE 0.0 END) {_JOURNEY}"),
    Metric("core_journey_p95_minutes", "minutes", "p95 duration of passing core-journey probes (24h)", timedelta(hours=24),
           f"SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY (payload->>'duration_s')::float) / 60.0 {_JOURNEY} AND ok AND payload ? 'duration_s'"),
    Metric("escrow_tasks_completed_7d", "count", "completed escrow tasks between distinct agents (7d)", timedelta(days=7),
           "SELECT COUNT(*) FROM task_sessions WHERE status = 'completed' AND escrow_amount > 0 AND caller_agent_id <> callee_agent_id AND completed_at >= :t"),
    Metric("a2a_agents_live_card", "count", "A2A agents whose card validated in the last 7 days with no failure since", timedelta(days=7),
           "SELECT COUNT(*) FROM a2a_remote_agents WHERE consecutive_failures = 0 AND last_validated_at >= :t"),
    Metric("bench_holdout_pass_at_1", "ratio", "holdout delivered pass@1 of main's latest bench report", timedelta(days=30),
           "SELECT (summary->'splits'->'holdout'->>'pass_at_1')::float FROM society_bench_reports WHERE revision = judge_revision "
           "AND created_at >= :t ORDER BY created_at DESC LIMIT 1"),
    Metric("usd_per_objective_pr_30d", "usd", "model spend per merged objective-linked ticket (30d)", timedelta(days=30),
           "SELECT (SELECT COALESCE(SUM(cost_usd), 0) FROM agent_runs WHERE created_at >= :t) / NULLIF((SELECT COUNT(*) FROM society_tickets WHERE merged_at >= :t), 0)"),
)}


def resolvable(metric_id: str) -> bool:
    return metric_id in REGISTRY


def read(db: Session, metric_id: str, now: Optional[datetime] = None) -> Optional[float]:
    m = REGISTRY[metric_id]
    v = db.execute(text(m.sql), {"t": (now or datetime.now(timezone.utc)) - m.window}).scalar()
    return None if v is None else round(float(v), 4)
