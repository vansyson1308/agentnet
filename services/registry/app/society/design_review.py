"""Design review: the company's meeting before an approved ticket is designed.

The protocol is deterministic; the models only contribute typed positions.

    ticket approved (owner or board)
      -> design_review.requested   (a fresh story)   Architect: PROPOSE_DESIGN
      -> design_review.proposed                      Scout, QA, Security: REVIEW_DESIGN
      -> design_review.ready  (all three, or the review window)   Governor: DECIDE_DESIGN
           go     -> company.ticket_approved (a fresh design story) carrying the minutes
           revise -> the next round (at most MAX_ROUNDS) with the Governor's feedback
           drop   -> the ticket is refused with the Governor's rationale

Rules the models cannot argue with:
- a Security ``object`` bars ``go`` (revise or drop only);
- the last round has no ``revise``;
- every contribution is also an AgentChat message in the review's thread
  (``thread_id`` = review id), so the meeting is readable as a conversation;
- stalls never strand a ticket (``sweep``): no proposal in time -> the ticket is
  designed without a review (said so in the minutes); reviews missing at the window
  -> the Governor decides with the positions it has; no decision in time -> ``go``
  unless anyone objected, then the ticket returns to ``proposed`` for re-planning.

Off unless ``SOCIETY_DESIGN_REVIEW_ENABLED``. Owner and incident tickets skip it.
Nothing here touches wallets, grants, secrets or the risk classifier.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .events import EventType, emit_event, utcnow

logger = logging.getLogger(__name__)

REVIEWERS = ("scout", "qa", "security")
MAX_ROUNDS = 2
PROPOSAL_WINDOW = timedelta(hours=2)
REVIEW_WINDOW = timedelta(hours=2)
DECISION_WINDOW = timedelta(hours=2)
OPEN_STATUSES = ("proposing", "reviewing", "deciding")


def enabled(settings: Any = None) -> bool:
    if settings is None:
        from .config import get_settings  # noqa: PLC0415

        settings = get_settings()
    return bool(getattr(settings, "design_review_enabled", False))


def _row(db: Session, where: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    r = db.execute(text(f"SELECT * FROM society_design_reviews WHERE {where} ORDER BY created_at DESC LIMIT 1"), params).mappings().first()  # noqa: S608
    return dict(r) if r else None


def get(db: Session, review_id: Any) -> Optional[Dict[str, Any]]:
    try:
        rid = uuid.UUID(str(review_id))
    except (TypeError, ValueError):
        return None
    return _row(db, "id = :id", {"id": rid})


def for_correlation(db: Session, correlation_id: Any) -> Optional[Dict[str, Any]]:
    """The review whose CURRENT round runs in this story."""
    return _row(db, "correlation_id = :c", {"c": correlation_id})


def _update(db: Session, review_id: Any, **cols: Any) -> None:
    sets = ", ".join(f"{k} = CAST(:{k} AS JSONB)" if k in ("proposal", "positions", "decision", "history") else f"{k} = :{k}" for k in cols)
    vals = {k: (json.dumps(v) if k in ("proposal", "positions", "decision", "history") else v) for k, v in cols.items()}
    db.execute(text(f"UPDATE society_design_reviews SET {sets}, updated_at = NOW() WHERE id = :id"), {"id": review_id, **vals})  # noqa: S608 -- column names are ours


def _ticket_brief(t: Dict[str, Any]) -> Dict[str, Any]:
    return {"ticket_id": str(t["id"]), "proposal_id": str(t["proposal_id"]), "title": t["title"], "objective_id": t["objective_id"],
            "metric_id": t["metric_id"], "direction": t["direction"], "expected_effect": float(t["expected_effect"] or 0), "proof": t["proof"]}


def open_review(db: Session, ticket: Dict[str, Any], *, approved_by: str) -> uuid.UUID:
    """The ticket was approved: open its review (round 1) instead of waking the Architect
    to design. The caller commits."""
    rid = uuid.uuid4()
    db.execute(text("INSERT INTO society_design_reviews (id, ticket_id, plan_id, approved_by, correlation_id) VALUES (:id, :t, :p, :by, :c)"),
               {"id": rid, "t": ticket["id"], "p": ticket.get("plan_id"), "by": approved_by, "c": uuid.uuid4()})
    _request_round(db, get(db, rid), ticket, feedback=None)
    return rid


def _request_round(db: Session, review: Dict[str, Any], ticket: Dict[str, Any], *, feedback: Optional[Dict[str, Any]]) -> None:
    emit_event(db, event_type=EventType.DESIGN_REVIEW_REQUESTED, actor_type="system", subject_type="proposal", subject_id=ticket["proposal_id"],
               correlation_id=review["correlation_id"],
               payload={"review_id": str(review["id"]), "round": int(review["round"]), **_ticket_brief(ticket), **({"feedback": feedback} if feedback else {})},
               idempotency_key=f"design_review.requested:{review['id']}:{review['round']}")


def _chat(db: Session, review: Dict[str, Any], agent: Any, title: str, content: str, message_type: str) -> None:
    from ..models import AgentChat, AgentMessageType  # noqa: PLC0415

    db.add(AgentChat(id=uuid.uuid4(), from_agent_id=agent.id, to_agent_id=None, message_type=AgentMessageType(message_type), title=title[:255],
                     content=content[:4000], thread_id=review["id"], is_read=False,
                     msg_metadata={"design_review_id": str(review["id"]), "round": int(review["round"])}))


def _ticket(db: Session, review: Dict[str, Any]) -> Dict[str, Any]:
    row = db.execute(text("SELECT * FROM society_tickets WHERE id = :id"), {"id": review["ticket_id"]}).mappings().one()
    return dict(row)


class ReviewRefused(Exception):
    pass


def _current(db: Session, review_id: Any, correlation_id: Any, status: str) -> Dict[str, Any]:
    review = get(db, review_id)
    if review is None:
        raise ReviewRefused("no such design review")
    if str(review["correlation_id"]) != str(correlation_id):
        raise ReviewRefused("this story is not the review's current round (engineering.company.design_review.id)")
    if review["status"] != status:
        raise ReviewRefused(f"design review is {review['status']} (round {review['round']}), not {status}")
    return review


def propose(db: Session, *, review_id: Any, correlation_id: Any, agent: Any, proposal: Dict[str, Any], causation: Any) -> Dict[str, Any]:
    review = _current(db, review_id, correlation_id, "proposing")
    _update(db, review["id"], proposal=proposal, positions={}, status="reviewing", stage_at=utcnow())
    review = get(db, review["id"])
    _chat(db, review, agent, f"Design proposal (round {review['round']}): {_ticket(db, review)['title']}",
          f"Approach: {proposal['approach']}\nFiles: {', '.join(proposal['files'])}\nProof: {proposal['proof']}\nRisks: {'; '.join(proposal.get('risks') or []) or 'none named'}",
          "proposal")
    emit_event(db, event_type=EventType.DESIGN_REVIEW_PROPOSED, actor_type="agent", actor_id=agent.id, causation=causation,
               payload={"review_id": str(review["id"]), "round": int(review["round"]), "ticket_id": str(review["ticket_id"])},
               idempotency_key=f"design_review.proposed:{review['id']}:{review['round']}")
    return review


def post_position(db: Session, *, review_id: Any, correlation_id: Any, agent: Any, role: str, position: Dict[str, Any], causation: Any) -> Dict[str, Any]:
    if role not in REVIEWERS:
        raise ReviewRefused(f"only {', '.join(REVIEWERS)} review a design")
    review = _current(db, review_id, correlation_id, "reviewing")
    positions = dict(review["positions"] or {})
    if role in positions:
        raise ReviewRefused(f"{role} already reviewed round {review['round']}")
    positions[role] = {**position, "agent": agent.name}
    _update(db, review["id"], positions=positions)
    review = get(db, review["id"])
    _chat(db, review, agent, f"Review by {role} (round {review['round']}): {position['stance']}", "\n".join(f"- {p}" for p in position["points"]),
          "review_result")
    if all(r in positions for r in REVIEWERS):
        _ready(db, review, causation=causation, why="all reviewers spoke")
    return review


def _ready(db: Session, review: Dict[str, Any], *, causation: Any, why: str) -> None:
    _update(db, review["id"], status="deciding", stage_at=utcnow())
    emit_event(db, event_type=EventType.DESIGN_REVIEW_READY, actor_type="system", causation=causation, correlation_id=review["correlation_id"],
               payload={"review_id": str(review["id"]), "round": int(review["round"]), "ticket_id": str(review["ticket_id"]),
                        "stances": {r: p["stance"] for r, p in (review["positions"] or {}).items()}, "why": why},
               idempotency_key=f"design_review.ready:{review['id']}:{review['round']}")


def decide(db: Session, *, review_id: Any, correlation_id: Any, agent: Any, decision: Dict[str, Any]) -> Dict[str, Any]:
    review = _current(db, review_id, correlation_id, "deciding")
    positions = review["positions"] or {}
    verdict = decision["verdict"]
    if verdict == "go" and (positions.get("security") or {}).get("stance") == "object":
        raise ReviewRefused("Security objected: the verdict can only be revise or drop")
    if verdict == "revise" and int(review["round"]) >= MAX_ROUNDS:
        raise ReviewRefused(f"round {review['round']} is the last: go or drop")
    _chat(db, review, agent, f"Decision (round {review['round']}): {verdict}",
          f"{decision['rationale']}" + ("\nConditions:\n" + "\n".join(f"- {c}" for c in decision.get("conditions") or []) if decision.get("conditions") else ""),
          "review_result")
    _settle(db, review, {**decision, "by": agent.name})
    return get(db, review["id"])


def _minutes(review: Dict[str, Any], decision: Dict[str, Any]) -> Dict[str, Any]:
    history = list(review.get("history") or [])
    return {"review_id": str(review["id"]), "rounds": int(review["round"]), "verdict": decision["verdict"], "rationale": decision.get("rationale"),
            "conditions": list(decision.get("conditions") or []), "by": decision.get("by"), "proposal": review.get("proposal"),
            "stances": {r: p["stance"] for r, p in (review.get("positions") or {}).items()}, "earlier_rounds": len(history)}


def _settle(db: Session, review: Dict[str, Any], decision: Dict[str, Any]) -> None:
    from . import tickets  # noqa: PLC0415

    t = _ticket(db, review)
    verdict = decision["verdict"]
    if verdict == "revise":
        history = list(review.get("history") or []) + [{"round": int(review["round"]), "proposal": review.get("proposal"),
                                                         "positions": review.get("positions"), "decision": decision}]
        _update(db, review["id"], history=history, round=int(review["round"]) + 1, status="proposing", proposal=None, positions={},
                correlation_id=uuid.uuid4(), stage_at=utcnow())
        feedback = {"rationale": decision.get("rationale"), "conditions": decision.get("conditions") or [],
                    "positions": {r: {"stance": p["stance"], "points": p["points"]} for r, p in (review.get("positions") or {}).items()}}
        _request_round(db, get(db, review["id"]), t, feedback=feedback)
        return
    minutes = _minutes(review, decision)
    _update(db, review["id"], decision=decision, status={"go": "go", "drop": "dropped", "return": "returned"}[verdict], stage_at=utcnow())
    if t["status"] != "approved":
        return  # the ticket moved on (owner, freshness close): the minutes stay on the review
    if verdict == "go":
        tickets.emit_approved(db, t, None, by=review["approved_by"] or "owner", design_review=minutes)
    elif verdict == "drop":
        tickets.set_status(db, t["id"], "refused", f"design review dropped it: {' '.join(str(decision.get('rationale') or '').split())}"[:500])
    else:  # return: nobody decided and someone objected -- the owner/board re-plans it
        tickets.set_status(db, t["id"], "proposed", f"design review: {decision.get('rationale')}"[:500], plan_id=None)


def sweep(db: Session, now: datetime) -> int:
    """No review strands a ticket. The caller commits. Returns reviews advanced."""
    n = 0
    rows = db.execute(text("SELECT * FROM society_design_reviews WHERE status IN ('proposing', 'reviewing', 'deciding') ORDER BY created_at FOR UPDATE SKIP LOCKED")).mappings().all()
    for r in rows:
        review = dict(r)
        age = now - review["stage_at"]
        if review["status"] == "proposing" and age >= PROPOSAL_WINDOW:
            _settle(db, review, {"verdict": "go", "rationale": f"no design proposal within {PROPOSAL_WINDOW.total_seconds() / 3600:g}h: designed without a review",
                                 "conditions": [], "by": "runtime"})
            n += 1
        elif review["status"] == "reviewing" and age >= REVIEW_WINDOW:
            if review["positions"]:
                _ready(db, review, causation=None, why=f"review window ({REVIEW_WINDOW.total_seconds() / 3600:g}h) elapsed; missing: "
                       + ", ".join(x for x in REVIEWERS if x not in review["positions"]))
            else:
                _settle(db, review, {"verdict": "go", "rationale": "no reviewer spoke within the window: designed as proposed", "conditions": [], "by": "runtime"})
            n += 1
        elif review["status"] == "deciding" and age >= DECISION_WINDOW:
            objections = [k for k, p in (review["positions"] or {}).items() if p.get("stance") == "object"]
            if objections:
                _settle(db, review, {"verdict": "return", "rationale": f"no decision in time and {', '.join(objections)} objected: returned for re-planning",
                                     "conditions": [], "by": "runtime"})
            else:
                _settle(db, review, {"verdict": "go", "rationale": "no decision in time and nobody objected", "conditions": [], "by": "runtime"})
            n += 1
    return n


def context_view(db: Session, correlation_id: Any) -> Dict[str, Any]:
    """What a role sees of the review its story belongs to (model-authored parts are untrusted)."""
    review = for_correlation(db, correlation_id)
    if review is None:
        return {}
    t = _ticket(db, review)
    step = {"proposing": "Architect: PROPOSE_DESIGN {review_id, approach, files, proof, risks}",
            "reviewing": "Scout, QA, Security: REVIEW_DESIGN {review_id, stance: support|concern|object, points} -- once each, about THIS proposal",
            "deciding": "Governor: DECIDE_DESIGN {review_id, verdict: go|revise|drop, rationale, conditions} -- Security object bars go; "
                        f"round {MAX_ROUNDS} has no revise"}.get(review["status"], "nothing: the review is settled")
    from . import work_packet  # noqa: PLC0415
    from .context import untrusted  # noqa: PLC0415

    return {"design_review": {
        "id": str(review["id"]), "round": int(review["round"]), "max_rounds": MAX_ROUNDS, "status": review["status"], "next": step,
        "ticket": _ticket_brief(t),
        "work_packet": work_packet.for_ticket(db, t),
        "proposal": untrusted(review["proposal"], source="agent:architect") if review["proposal"] else None,
        "positions": untrusted(review["positions"] or {}, source="agent:reviewers"),
        "earlier_rounds": untrusted(review["history"] or [], source="agent:review_history"),
    }}


def minutes_for_ticket(db: Session, ticket_id: Any) -> Optional[Dict[str, Any]]:
    """The settled review minutes the design story carries (latest review of the ticket)."""
    r = _row(db, "ticket_id = :t AND status IN ('go', 'dropped', 'returned')", {"t": ticket_id})
    return _minutes(r, r["decision"] or {}) if r else None


def view(db: Session, limit: int = 20) -> List[Dict[str, Any]]:
    """Operator view: the meetings and their minutes."""
    rows = db.execute(text("SELECT r.*, t.title FROM society_design_reviews r JOIN society_tickets t ON t.id = r.ticket_id ORDER BY r.created_at DESC LIMIT :n"),
                      {"n": limit}).mappings().all()
    out = []
    for r in rows:
        out.append({"id": str(r["id"]), "ticket_id": str(r["ticket_id"]), "title": r["title"], "status": r["status"], "round": r["round"],
                    "approved_by": r["approved_by"], "proposal": r["proposal"], "positions": r["positions"], "decision": r["decision"],
                    "history": r["history"], "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat()})
    return out


def counts(db: Session) -> Dict[str, int]:
    """Structural counts (safe for public/metrics surfaces)."""
    return {k: int(v) for k, v in db.execute(text("SELECT status, COUNT(*) FROM society_design_reviews GROUP BY status")).all()}
