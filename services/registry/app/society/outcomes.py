"""Outcomes: did a merged ticket move its key result? (analytics department, no model).

A ticket whose candidate's promotion merged becomes ``merged``; its KR metric
is read from the deterministic source (``metrics.py``) at merge, at +24h and
at +7d. The outcome compares the latest reading with the one at merge, in the
KR's direction, against a tolerance of 10% of the ticket's expected effect:
``moved`` (better), ``regressed`` (worse) or ``no_effect``. The +24h outcome is
provisional; +7d is final. No reading (no data) records no outcome.

KPIs for the operator /company view and a Telegram-ready summary: KR progress
per active objective, tickets by outcome, meaningful rate (moved / decided) and
model $ per meaningful (moved) ticket.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import charter, metrics

TOLERANCE = 0.10
WINDOWS = (("metric_24h", timedelta(hours=24)), ("metric_7d", timedelta(days=7)))


def outcome(direction: str, at_merge: Optional[float], now: Optional[float], expected: float) -> Optional[str]:
    if at_merge is None or now is None:
        return None
    gain = (now - at_merge) * (1 if direction == "up" else -1)
    tol = max(1e-9, TOLERANCE * abs(float(expected or 0)))
    return "moved" if gain > tol else "regressed" if gain < -tol else "no_effect"


def record(db: Session, now: datetime) -> int:
    """Mark merged tickets and record due readings. Idempotent; the caller commits."""
    changed = 0
    merged = db.execute(text(
        "SELECT t.id, t.metric_id, p.updated_at FROM society_tickets t JOIN code_promotions p ON p.candidate_id = t.candidate_id "
        "WHERE t.status IN ('building', 'ready') AND p.merged_sha IS NOT NULL")).all()
    for tid, metric_id, at in merged:
        db.execute(text("UPDATE society_tickets SET status = 'merged', merged_at = :at, metric_at_merge = :m, updated_at = NOW() WHERE id = :id"),
                   {"id": tid, "at": at or now, "m": metrics.read(db, metric_id, now)})
        changed += 1
    for col, delay in WINDOWS:
        due = db.execute(text(f"SELECT id, metric_id, direction, expected_effect, metric_at_merge FROM society_tickets "  # noqa: S608 -- our column names
                              f"WHERE status = 'merged' AND {col} IS NULL AND merged_at <= :t"), {"t": now - delay}).mappings().all()
        for t in due:
            value = metrics.read(db, t["metric_id"], now)
            if value is None:
                continue
            base = float(t["metric_at_merge"]) if t["metric_at_merge"] is not None else None
            db.execute(text(f"UPDATE society_tickets SET {col} = :v, outcome = COALESCE(:o, outcome), updated_at = NOW() WHERE id = :id"),  # noqa: S608
                       {"v": value, "o": outcome(t["direction"], base, value, float(t["expected_effect"] or 0)), "id": t["id"]})
            changed += 1
    return changed


def kpis(db: Session, now: datetime) -> Dict[str, Any]:
    krs: List[Dict[str, Any]] = []
    for o in charter.objectives(db):
        if o["status"] != "active":
            continue
        for kr in o["key_results"]:
            cur = metrics.read(db, kr["metric_id"], now)
            base = kr.get("baseline")
            span = float(kr["target"]) - float(base) if base is not None else None
            progress = round((cur - float(base)) / span, 3) if cur is not None and span else None
            krs.append({"objective_id": o["id"], "metric_id": kr["metric_id"], "baseline": base, "current": cur, "target": kr["target"],
                        "direction": kr["direction"], "progress": progress})
    by = dict(db.execute(text("SELECT COALESCE(outcome, 'pending'), COUNT(*) FROM society_tickets WHERE status = 'merged' GROUP BY 1")).all())
    moved = int(by.get("moved", 0))
    decided = moved + int(by.get("no_effect", 0)) + int(by.get("regressed", 0))
    spend = float(db.execute(text("SELECT COALESCE(SUM(cost_usd), 0) FROM society_tickets WHERE status = 'merged'")).scalar() or 0)
    out = {"key_results": krs, "tickets_by_outcome": {k: int(v) for k, v in by.items()},
           "meaningful_rate": round(moved / decided, 3) if decided else None,
           "usd_per_meaningful_pr": round(spend / moved, 4) if moved else None}
    out["summary_text"] = summary(out)
    return out


def summary(k: Dict[str, Any]) -> str:
    """Plain text an owner can read on a phone (Telegram-ready): one line per KR, then the totals."""
    lines = [f"{r['objective_id']} {r['metric_id']}: {r['current'] if r['current'] is not None else 'no data'} -> {r['target']} ({r['direction']})"
             + (f", progress {round(100 * r['progress'])}%" if r["progress"] is not None else "") for r in k["key_results"]] or ["no active objective"]
    t = k["tickets_by_outcome"]
    lines.append(f"merged tickets: moved {t.get('moved', 0)}, no effect {t.get('no_effect', 0)}, regressed {t.get('regressed', 0)}, pending {t.get('pending', 0)}")
    lines.append(f"meaningful rate: {k['meaningful_rate'] if k['meaningful_rate'] is not None else 'n/a'}; $ per meaningful PR: {k['usd_per_meaningful_pr'] if k['usd_per_meaningful_pr'] is not None else 'n/a'}")
    return "\n".join(lines)
