"""Tickets: the unit of company work and the meaning gate (deterministic, no model).

A ticket rides on one ImprovementProposal and says why the work exists: an
ACTIVE charter objective, one of its key-result metrics (number + direction)
and a proof (acceptance test node ids, or ``probe:<metric_id>``). In company
mode no candidate is created without one that passes ``check``; a proof that
already passes on the base revision closes it ``already_satisfied``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from . import charter, metrics

DUPLICATE_WINDOW_DAYS = 14
LIVE_STATUSES = ("approved", "building", "ready", "merged")
DEPARTMENT_BY_ROLE = {"governor": "ceo_office", "scout": "strategy_product", "architect": "engineering", "builder": "engineering", "qa": "quality", "security": "security", "evaluator": "analytics"}


class GateRefused(Exception):
    pass


def create(db: Session, *, proposal_id: uuid.UUID, title: str, fields: Dict[str, Any], source: str, role: str, importance: int) -> uuid.UUID:
    tid = uuid.uuid4()
    db.execute(text(
        "INSERT INTO society_tickets (id, proposal_id, title, objective_id, metric_id, expected_effect, direction, proof, source, priority, department) "
        "VALUES (:id, :p, :t, :o, :m, :e, :d, CAST(:proof AS JSONB), :s, :pr, :dep)"),
        {"id": tid, "p": proposal_id, "t": title[:255], "o": fields["objective_id"], "m": fields["metric_id"], "e": fields["expected_effect"],
         "d": fields["direction"], "proof": json.dumps(list(fields.get("proof") or [])), "s": source, "pr": max(0, min(5, importance // 20)),
         "dep": DEPARTMENT_BY_ROLE.get(role, "strategy_product")})
    return tid


def for_proposal(db: Session, proposal_id: Optional[uuid.UUID]) -> Optional[Dict[str, Any]]:
    if proposal_id is None:
        return None
    row = db.execute(text("SELECT * FROM society_tickets WHERE proposal_id = :p"), {"p": proposal_id}).mappings().first()
    return dict(row) if row else None


def set_status(db: Session, ticket_id: Any, status: str, reason: Optional[str] = None, **cols: Any) -> None:
    sets = "".join(f", {k} = :{k}" for k in cols)
    db.execute(text(f"UPDATE society_tickets SET status = :s, reason = COALESCE(:r, reason), updated_at = NOW(){sets} WHERE id = :id"),  # noqa: S608 -- column names are ours
               {"s": status, "r": reason[:500] if reason else None, "id": ticket_id, **cols})


def add_cost(db: Session, ticket_id: Any, usd: Any) -> None:
    db.execute(text("UPDATE society_tickets SET cost_usd = cost_usd + :c, updated_at = NOW() WHERE id = :id"), {"c": float(usd or 0), "id": ticket_id})


def proof_tests(ticket: Dict[str, Any]) -> List[str]:
    return [p for p in (ticket.get("proof") or []) if isinstance(p, str) and not p.startswith("probe:")]


def check(db: Session, proposal_id: Optional[uuid.UUID], spec: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """The meaning gate. Returns the ticket (now ``building``) or raises GateRefused
    after recording the refusal on the ticket (the caller commits)."""
    t = for_proposal(db, proposal_id)
    if t is None:
        raise GateRefused("company mode: the proposal has no ticket (objective_id, metric_id, expected_effect, direction, proof)")

    def refuse(why: str) -> None:
        set_status(db, t["id"], "refused", why)
        raise GateRefused(f"ticket refused: {why}")

    files = sorted(str(f) for f in spec.get("files_allowed") or [])
    if charter.CHARTER_REPO_PATH in files:
        refuse("the company charter is owner-edited only")
    obj = charter.find(db, str(t.get("objective_id") or ""))
    if obj is None or obj["status"] != "active":
        refuse(f"objective {t.get('objective_id')!r} is not active")
    if t.get("metric_id") not in [kr["metric_id"] for kr in obj["key_results"]] or not metrics.resolvable(str(t.get("metric_id"))):
        refuse(f"metric {t.get('metric_id')!r} is not a key result of {obj['id']}")
    proof = sorted(t.get("proof") or [])
    if not proof:
        refuse("no proof (acceptance test node ids or probe:<metric_id>)")
    missing = [p for p in proof_tests(t) if p not in (spec.get("acceptance_tests") or [])]
    if missing:
        refuse(f"the spec does not run the proof {missing}")
    dup = db.execute(text(
        "SELECT id FROM society_tickets WHERE id <> :id AND status IN :live AND updated_at >= :since AND (status <> 'building' OR candidate_id IN "
        "(SELECT id FROM code_candidates WHERE status NOT IN ('rejected', 'failed', 'abandoned'))) "
        "AND proof = CAST(:proof AS JSONB) AND files = CAST(:files AS JSONB) LIMIT 1").bindparams(bindparam("live", expanding=True)),
        {"id": t["id"], "live": list(LIVE_STATUSES), "since": now - timedelta(days=DUPLICATE_WINDOW_DAYS),
         "proof": json.dumps(proof), "files": json.dumps(files)}).scalar()
    if dup is not None:
        refuse(f"duplicate of ticket {dup} (same proof and files within {DUPLICATE_WINDOW_DAYS} days)")
    db.execute(text("UPDATE society_tickets SET proof = CAST(:proof AS JSONB), files = CAST(:files AS JSONB) WHERE id = :id"),
               {"id": t["id"], "proof": json.dumps(proof), "files": json.dumps(files)})
    set_status(db, t["id"], "building")
    return {**t, "objective": obj}


def link_text(ticket: Dict[str, Any]) -> str:  # the objective link the PR body carries (spec expected_effect)
    return f"[{ticket['objective_id']} {ticket['metric_id']} {ticket['direction']} {float(ticket['expected_effect']):g}]"
