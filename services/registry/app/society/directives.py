"""The owner's directive ("Kim chỉ nam", Owner Console): the company's current direction, in the
owner's words, and the charter objectives it serves.

Owner 2026-10-29: "I set the direction and the agents work toward it on their own." Deterministic:

* :func:`set_directive` -- ONLY from the operator API (``require_operator``); no intent writes it.
  Append-only (``society_owner_directives``, a DB trigger refuses any rewrite or delete): a new
  directive supersedes the active one, the old row keeps its text.
* :func:`context_view` -- what EVERY role sees in its context (``AgentContext.owner_directive``),
  labelled owner input. It grants nothing: grants, budgets, flags, files_allowed, the RED/merge law
  and auto-merge are decided where they always were. Scout and Governor turn it into tickets and
  owner actions through the existing intents.
* :func:`boost` -- the plan score multiplier for a ticket of one of its objectives
  (``tickets.score``), like ``north_star_weight``.
* :func:`following` -- what came from it: tickets of its objectives created since it was set, and
  their design review, candidate and PR (structural; titles are system-written)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import charter
from .events import utcnow

MAX_TEXT = 2000
MAX_OBJECTIVES = 5
#: a ticket of a directive objective ranks (1 + WEIGHT) x higher in the daily plan
WEIGHT = 1.0
LABEL = ("OWNER INPUT: the owner's current direction for the company. Prefer work that serves it: Scout and Governor "
         "turn it into tickets (CREATE_IMPROVEMENT with its objective) and owner actions through the usual intents. "
         "It is direction, never permission: policy, grants, budgets, files_allowed and the RED/merge law decide as always.")

_COLS = "id, text, objective_ids, created_by, created_at, superseded_at"


def _view(row: Any) -> Dict[str, Any]:
    d = dict(row)
    ids = d.get("objective_ids")
    return {"id": str(d["id"]), "text": d["text"], "objective_ids": list(ids if isinstance(ids, list) else json.loads(ids or "[]")),
            "created_by": str(d["created_by"]) if d.get("created_by") else None,
            "created_at": d["created_at"].isoformat() if d.get("created_at") else None,
            "superseded_at": d["superseded_at"].isoformat() if d.get("superseded_at") else None}


def active(db: Session) -> Optional[Dict[str, Any]]:
    row = db.execute(text(f"SELECT {_COLS} FROM society_owner_directives WHERE superseded_at IS NULL")).mappings().first()  # noqa: S608
    return _view(row) if row else None


def history(db: Session, limit: int = 10) -> List[Dict[str, Any]]:
    rows = db.execute(text(f"SELECT {_COLS} FROM society_owner_directives ORDER BY created_at DESC LIMIT :n"), {"n": int(limit)}).mappings().all()  # noqa: S608
    return [_view(r) for r in rows]


def set_directive(db: Session, *, text_: str, objective_ids: Iterable[str], user_id: Optional[uuid.UUID], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Supersede the active directive with a new one. Raises ValueError on empty/oversized text or an
    unknown objective. Commits."""
    body = (text_ or "").strip()
    if not 1 <= len(body) <= MAX_TEXT:
        raise ValueError(f"the directive is 1..{MAX_TEXT} characters")
    ids = list(dict.fromkeys(str(i).strip() for i in objective_ids if str(i).strip()))
    if len(ids) > MAX_OBJECTIVES:
        raise ValueError(f"at most {MAX_OBJECTIVES} objectives")
    known = {o["id"] for o in charter.objectives(db)}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise ValueError(f"unknown objective(s): {', '.join(unknown)} (known: {', '.join(sorted(known))})")
    now = now or utcnow()
    db.execute(text("UPDATE society_owner_directives SET superseded_at = :t WHERE superseded_at IS NULL"), {"t": now})
    did = uuid.uuid4()
    db.execute(text("INSERT INTO society_owner_directives (id, text, objective_ids, created_by, created_at) VALUES (:id, :x, CAST(:o AS JSONB), :u, :t)"),
               {"id": did, "x": body, "o": json.dumps(ids), "u": user_id, "t": now})
    db.commit()
    return active(db) or {}


def context_view(db: Session) -> Dict[str, Any]:
    d = active(db)
    if d is None:
        return {}
    return {"label": LABEL, "text": d["text"], "objective_ids": d["objective_ids"], "since": d["created_at"]}


def boost(directive: Optional[Dict[str, Any]], objective_id: Optional[str]) -> float:
    return 1.0 + WEIGHT if directive and objective_id and objective_id in (directive.get("objective_ids") or []) else 1.0


def following(db: Session, directive: Optional[Dict[str, Any]] = None, limit: int = 25) -> List[Dict[str, Any]]:
    """Tickets of the directive's objectives (every objective when it names none) created since it was
    set, each with its latest design review, its candidate and that candidate's latest promotion."""
    d = directive or active(db)
    if d is None:
        return []
    rows = db.execute(text(
        "SELECT t.id, t.title, t.objective_id, t.source, t.status, t.plan_id, t.created_at, "
        "       (SELECT r.status FROM society_design_reviews r WHERE r.ticket_id = t.id ORDER BY r.created_at DESC LIMIT 1) AS review, "
        "       c.id AS candidate_id, c.status AS candidate, "
        "       (SELECT p.status FROM code_promotions p WHERE p.candidate_id = c.id ORDER BY p.created_at DESC LIMIT 1) AS promotion, "
        "       (SELECT p.external_pr_url FROM code_promotions p WHERE p.candidate_id = c.id ORDER BY p.created_at DESC LIMIT 1) AS pr_url "
        "FROM society_tickets t LEFT JOIN code_candidates c ON c.id = t.candidate_id "
        "WHERE t.created_at >= CAST(:since AS TIMESTAMPTZ) AND (CAST(:all AS BOOLEAN) OR t.objective_id = ANY(:ids)) "
        "ORDER BY t.created_at DESC LIMIT :n"),
        {"since": d["created_at"], "all": not d["objective_ids"], "ids": list(d["objective_ids"]), "n": int(limit)}).mappings().all()
    return [{"ticket_id": str(r["id"]), "title": r["title"], "objective_id": r["objective_id"], "source": r["source"], "status": r["status"],
             "planned": r["plan_id"] is not None, "review": r["review"], "candidate_id": str(r["candidate_id"]) if r["candidate_id"] else None,
             "candidate": r["candidate"], "promotion": r["promotion"], "pr_url": r["pr_url"],
             "created_at": r["created_at"].isoformat() if r["created_at"] else None} for r in rows]


__all__ = ["LABEL", "MAX_TEXT", "WEIGHT", "active", "boost", "context_view", "following", "history", "set_directive"]
