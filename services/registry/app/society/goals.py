"""The north star: the one number the company exists to move (deterministic, no model).

The owner writes it in ``company_charter.json`` (``north_star``: revenue toward
$1,000,000) and weighs every key result toward it (``north_star_weight``, 0..1).
With ``SOCIETY_NORTH_STAR_ENABLED`` on:

* the daily plan ranks a ticket by ``(1 + weight)`` on top of priority x impact
  / cost (``tickets.score``), so revenue-moving work comes first;
* every role sees the scorecard (``context_view``): where revenue stands, the
  pace the deadline needs, and which key results weigh most;
* the operator status shows how much model spend went to objective-linked
  tickets (``scorecard`` ``spend``), so busywork is visible as a number.

Revenue is real money only (``metrics.py``): USDC platform fees the
transactions trigger already took, plus external income an operator recorded
with its reference (``record_revenue``). That ledger is record-only: it never
touches wallets or transactions. Nothing here acts outside AgentNet -- posting
in public, joining outside job platforms or spending money stay owner actions
(docs/NORTH_STAR.md).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import charter, metrics

REVENUE_SOURCES = ("external_job", "sponsorship", "subscription", "other")
#: The 30-day revenue metric the pace is read from.
PACE_METRIC = "revenue_usd_30d"
CONTEXT_RULE = (
    "The north star is why the company exists: every intent should move a key result, and the plan ranks "
    "work by its key result's north_star_weight. Prefer the heaviest key result you can move with evidence; "
    "when nothing you can do moves one, 'no high-value change' is the answer -- never work to look busy. "
    "Actions outside AgentNet (public posts, signing up on outside job platforms, emailing people, spending "
    "money) need the owner's accounts and approval: they are not intents and you never claim to have done them."
)


def enabled(settings: Any = None) -> bool:
    if settings is None:
        from .config import get_settings  # noqa: PLC0415

        settings = get_settings()
    return bool(getattr(settings, "north_star_enabled", False))


def north_star(doc: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    return (doc or charter.load()).get("north_star")


def weight(obj: Dict[str, Any], metric_id: Any) -> float:
    """How much the objective's key result weighs toward the north star (0 when unknown)."""
    kr = next((k for k in obj.get("key_results") or [] if k["metric_id"] == metric_id), None)
    return float((kr or {}).get("north_star_weight") or 0.0)


def _met(current: Optional[float], kr: Dict[str, Any]) -> bool:
    if current is None:
        return False
    return current >= float(kr["target"]) if kr["direction"] == "up" else current <= float(kr["target"])


def _pace(ns: Dict[str, Any], current: Optional[float], last_30d: Optional[float], today: date) -> Dict[str, Any]:
    deadline = date.fromisoformat(str(ns["deadline"]))
    days_left = max(1, (deadline - today).days)
    gap = max(0.0, float(ns["target"]) - float(current or 0))
    needed, pace = round(gap / days_left, 2), round(float(last_30d or 0) / 30.0, 2)
    return {"days_left": days_left, "gap_usd": round(gap, 2), "needed_usd_per_day": needed, "pace_usd_per_day_30d": pace,
            "on_pace": gap == 0 or pace >= needed}


def _spend(db: Session, now: datetime) -> Dict[str, Any]:
    """Model spend in the last 30 days, and the share that went to objective-linked tickets,
    weighted by each ticket's key-result weight toward the north star."""
    since = now - timedelta(days=30)
    total = float(db.execute(text("SELECT COALESCE(SUM(cost_usd), 0) FROM agent_runs WHERE created_at >= :t"), {"t": since}).scalar() or 0)
    rows = db.execute(text("SELECT objective_id, metric_id, COALESCE(SUM(cost_usd), 0) FROM society_tickets WHERE created_at >= :t "
                           "GROUP BY objective_id, metric_id"), {"t": since}).all()
    objs = {o["id"]: o for o in charter.objectives(db)}
    on_tickets = sum(float(c) for _, _, c in rows)
    weighted = sum(float(c) * weight(objs.get(o) or {}, m) for o, m, c in rows)
    return {"model_usd_30d": round(total, 4), "on_tickets_usd_30d": round(on_tickets, 4),
            "north_star_weighted_usd_30d": round(weighted, 4),
            "ticket_share": round(on_tickets / total, 3) if total else None}


def scorecard(db: Session, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The operator's view: the north star, its pace, every key result and its weight, the spend."""
    now = now or datetime.now(timezone.utc)
    ns = north_star()
    if ns is None:
        return {"north_star": None}
    current = metrics.read(db, ns["metric_id"], now)
    last_30d = metrics.read(db, PACE_METRIC, now)
    krs: List[Dict[str, Any]] = []
    for o in charter.objectives(db):
        for kr in o["key_results"]:
            cur = metrics.read(db, kr["metric_id"], now) if o["status"] == "active" else None
            krs.append({"objective_id": o["id"], "status": o["status"], "owner_priority": o["owner_priority"], "metric_id": kr["metric_id"],
                        "current": cur, "target": kr["target"], "direction": kr["direction"], "weight": float(kr.get("north_star_weight") or 0),
                        "met": _met(cur, kr)})
    krs.sort(key=lambda k: (k["status"] != "active", -k["weight"], k["owner_priority"]))
    out = {"north_star": {**{k: ns[k] for k in ("metric_id", "target", "direction", "deadline", "statement")}, "current_usd": current or 0.0,
                          "last_30d_usd": last_30d or 0.0, **_pace(ns, current, last_30d, now.date())},
           "key_results": krs, "spend": _spend(db, now), "enabled": enabled()}
    out["summary_text"] = summary(out)
    return out


def summary(card: Dict[str, Any]) -> str:
    """Plain text an owner can read on a phone."""
    ns = card.get("north_star")
    if not ns:
        return "no north star in the charter"
    lines = [f"North star: ${ns['current_usd']:,.2f} / ${float(ns['target']):,.0f} by {ns['deadline']} "
             f"(30d ${ns['last_30d_usd']:,.2f}; pace ${ns['pace_usd_per_day_30d']:,.2f}/day, needed ${ns['needed_usd_per_day']:,.2f}/day"
             f"{'' if ns['on_pace'] else ', BEHIND'})"]
    for k in card["key_results"]:
        if k["status"] == "active":
            lines.append(f"{k['objective_id']} {k['metric_id']} (w {k['weight']:g}): "
                         f"{k['current'] if k['current'] is not None else 'no data'} -> {k['target']}{' met' if k['met'] else ''}")
    s = card["spend"]
    share = f"{round(100 * s['ticket_share'])}%" if s["ticket_share"] is not None else "n/a"
    lines.append(f"model spend 30d ${s['model_usd_30d']:,.2f}, on objective tickets {share}")
    return "\n".join(lines)


def context_view(db: Session, now: Optional[datetime] = None) -> Dict[str, Any]:
    """What every role sees (bounded: one number set + the active key results by weight)."""
    card = scorecard(db, now)
    ns = card["north_star"]
    if ns is None:
        return {}
    return {"north_star": {"statement": ns["statement"], "target_usd": ns["target"], "deadline": ns["deadline"], "current_usd": ns["current_usd"],
                           "last_30d_usd": ns["last_30d_usd"], "needed_usd_per_day": ns["needed_usd_per_day"], "on_pace": ns["on_pace"],
                           "key_results_by_weight": [{k: r[k] for k in ("objective_id", "metric_id", "weight", "current", "target", "met")}
                                                     for r in card["key_results"] if r["status"] == "active"][:8],
                           "rule": CONTEXT_RULE}}


def record_revenue(db: Session, *, source: str, amount_usd: Any, reference: str, received_at: Optional[datetime] = None,
                   note: Optional[str] = None, operator_id: Any = None) -> Dict[str, Any]:
    """An operator records income earned outside escrow. Idempotent per ``reference``: the same
    reference with the same amount returns the existing entry; a different amount is refused.
    Record-only -- no wallet or transaction is touched. The caller commits."""
    if source not in REVENUE_SOURCES:
        raise ValueError(f"source must be one of {REVENUE_SOURCES}")
    amount = Decimal(str(amount_usd)).quantize(Decimal("0.01"))
    if not Decimal("0") < amount <= Decimal("1000000"):
        raise ValueError("amount_usd must be > 0 and <= 1,000,000")
    reference = str(reference or "").strip()
    if not reference:
        raise ValueError("a reference (invoice, payout or contract id) is required")
    existing = db.execute(text("SELECT * FROM society_revenue_entries WHERE reference = :r"), {"r": reference}).mappings().first()
    if existing is not None:
        if Decimal(existing["amount_usd"]) != amount or existing["source"] != source:
            raise ValueError(f"reference {reference!r} is already recorded with a different amount or source")
        return _entry(existing)
    row = db.execute(text("INSERT INTO society_revenue_entries (id, source, amount_usd, reference, note, received_at, recorded_by) "
                          "VALUES (:id, :s, :a, :r, :n, :at, :by) RETURNING *"),
                     {"id": uuid.uuid4(), "s": source, "a": amount, "r": reference[:255], "n": (note or "")[:500] or None,
                      "at": received_at or datetime.now(timezone.utc), "by": operator_id}).mappings().first()
    return _entry(row)


def _entry(r: Any) -> Dict[str, Any]:
    return {"id": str(r["id"]), "source": r["source"], "amount_usd": float(r["amount_usd"]), "reference": r["reference"], "note": r["note"],
            "received_at": r["received_at"].isoformat(), "created_at": r["created_at"].isoformat()}


def revenue_view(db: Session, limit: int = 50) -> List[Dict[str, Any]]:
    rows = db.execute(text("SELECT * FROM society_revenue_entries ORDER BY received_at DESC LIMIT :n"), {"n": limit}).mappings().all()
    return [_entry(r) for r in rows]
