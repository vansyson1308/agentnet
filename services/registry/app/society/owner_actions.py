"""The owner action queue: work that moves a key result but happens OUTSIDE the repo (deterministic store).

Posting in public, listing AgentNet in directories, opening an account on an outside job platform or
setting up payments act in the owner's name, with the owner's accounts and money. No agent may do
them (docs/NORTH_STAR.md §6). They become items here instead, each with a one-line reason and an
expected effect:

* ``catalog`` items -- a fixed list per ACTIVE north-star objective, supplied at each company cycle;
* ``agent`` items -- a Scout or Governor ``PROPOSE_OWNER_ACTION`` (model-authored text: operator
  views only; at most ``AGENT_ITEMS_PER_DAY`` open per day, deduplicated by title).

Only an operator closes an item (done / dismissed). Nothing here sends, posts or pays.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import charter

AGENT_ITEMS_PER_DAY = 3
STATUSES = ("open", "done", "dismissed")

#: (objective, key result) -> the outside actions the company cannot take itself.
CATALOG: Dict[str, List[Dict[str, str]]] = {
    "O4": [
        {"key": "usdc-payout-path", "metric_id": "revenue_usd_30d", "title": "Set up a USDC payout/on-ramp path for agent owners",
         "reason": "needs the company's payment account and KYC", "expected_effect": "USDC platform fees become withdrawable revenue; sellers can be paid out"},
        {"key": "outside-job-platform", "metric_id": "external_jobs_paid_30d", "title": "Open one outside job-platform account for paid agent work",
         "reason": "an account in your name, KYC, and most platforms forbid bot-run accounts", "expected_effect": "paid external jobs, recorded in the revenue ledger"},
    ],
    "O5": [
        {"key": "a2a-directory-listing", "metric_id": "outside_agents_joined_7d", "title": "List AgentNet's agent cards in public A2A directories",
         "reason": "needs your go-ahead and the directory URLs (A2A_DIRECTORY_REGISTER_URLS)", "expected_effect": "outside agent developers find AgentNet and register"},
        {"key": "launch-post", "metric_id": "verified_signups_7d", "title": "Post the AgentNet launch (Hacker News / Product Hunt / X)",
         "reason": "a public post in your name", "expected_effect": "visitors and verified signups"},
    ],
}


def _key(title: str) -> str:
    return "agent:" + hashlib.sha256(" ".join(title.lower().split()).encode()).hexdigest()[:32]


def _insert(db: Session, *, key: str, item: Dict[str, Any], objective_id: str, source: str, agent_id: Any = None) -> Optional[str]:
    row = db.execute(text(
        "INSERT INTO society_owner_actions (id, key, title, reason, expected_effect, objective_id, metric_id, source, proposed_by_agent_id) "
        "VALUES (:id, :k, :t, :r, :e, :o, :m, :s, :a) ON CONFLICT (key) DO NOTHING RETURNING id"),
        {"id": uuid.uuid4(), "k": key[:160], "t": item["title"][:200], "r": item["reason"][:300], "e": item["expected_effect"][:300],
         "o": objective_id, "m": item.get("metric_id"), "s": source, "a": agent_id}).scalar()
    return str(row) if row else None


def supply_catalog(db: Session) -> int:
    """The catalog items of every ACTIVE objective (idempotent per key). The caller commits."""
    active = {o["id"] for o in charter.objectives(db) if o["status"] == "active"}
    return sum(1 for oid, items in CATALOG.items() if oid in active
               for item in items if _insert(db, key=f"catalog:{item['key']}", item=item, objective_id=oid, source="catalog"))


def propose(db: Session, *, agent_id: Any, title: str, reason: str, expected_effect: str, metric_id: str, now: datetime) -> Dict[str, Any]:
    """An agent's outside-action proposal for a key result of an ACTIVE objective. Raises ValueError
    when refused. The caller commits."""
    obj = next((o for o in charter.objectives(db) if o["status"] == "active" and metric_id in [k["metric_id"] for k in o["key_results"]]), None)
    if obj is None:
        raise ValueError(f"{metric_id} is not a key result of an active objective")
    n = db.execute(text("SELECT COUNT(*) FROM society_owner_actions WHERE source = 'agent' AND created_at >= :t"),
                   {"t": now.replace(hour=0, minute=0, second=0, microsecond=0)}).scalar() or 0
    if n >= AGENT_ITEMS_PER_DAY:
        raise ValueError(f"{n} owner action(s) were proposed today (at most {AGENT_ITEMS_PER_DAY})")
    item = {"title": title, "reason": reason, "expected_effect": expected_effect, "metric_id": metric_id}
    made = _insert(db, key=_key(title), item=item, objective_id=obj["id"], source="agent", agent_id=agent_id)
    return {"id": made, "duplicate": made is None, "objective_id": obj["id"]}


def decide(db: Session, action_id: uuid.UUID, status: str, *, user_id: Any, note: Optional[str] = None) -> Dict[str, Any]:
    if status not in ("done", "dismissed"):
        raise ValueError("an owner action is closed as done or dismissed")
    row = db.execute(text("UPDATE society_owner_actions SET status = :s, note = :n, decided_at = NOW(), decided_by = :u "
                          "WHERE id = :id AND status = 'open' RETURNING *"), {"s": status, "n": (note or "")[:300] or None, "u": user_id, "id": action_id}).mappings().first()
    if row is None:
        raise KeyError("no open owner action with this id")
    return _view(row)


def _view(r: Any) -> Dict[str, Any]:
    return {k: (str(v) if isinstance(v, uuid.UUID) else v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()}


def listing(db: Session, *, status: str = "open", limit: int = 50) -> List[Dict[str, Any]]:
    rows = db.execute(text("SELECT * FROM society_owner_actions WHERE status = :s ORDER BY created_at DESC LIMIT :n"), {"s": status, "n": limit}).mappings().all()
    return [_view(r) for r in rows]


def open_count(db: Session, since: Optional[datetime] = None) -> int:
    q = "SELECT COUNT(*) FROM society_owner_actions WHERE status = 'open'" + (" AND created_at >= :t" if since else "")
    return int(db.execute(text(q), {"t": since} if since else {}).scalar() or 0)


def summary_lines(db: Session, limit: int = 5) -> List[str]:
    """One line per open item, oldest first: what, why only you, expected effect."""
    rows = db.execute(text("SELECT objective_id, title, reason, expected_effect FROM society_owner_actions WHERE status = 'open' "
                           "ORDER BY created_at LIMIT :n"), {"n": limit}).all()
    return [f"- [{o}] {t} -- why you: {r}; effect: {e}" for o, t, r, e in rows]

