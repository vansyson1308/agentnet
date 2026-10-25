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
- every stage change wakes exactly the roles that must act, by name: one targeted
  system event per role agent (``_wake``; runs.TARGETED_ONLY_EVENT_TYPES), so each
  role gets its own run in the review's story;
- a Security ``object`` bars ``go`` (revise or drop only);
- the last round has no ``revise``;
- every contribution is also an AgentChat message in the review's thread
  (``thread_id`` = review id), so the meeting is readable as a conversation;
- each wake asks ITS role the stage's question by name, with a deadline, and the
  story offers only that stage's intent (context._story_permissions): reviewers
  REVIEW_DESIGN (stance ``abstain`` when they have nothing specific), the Governor
  DECIDE_DESIGN with every position in its wake; after ``go`` the Architect is
  offered the design (REQUEST_CODE_CHANGE), not another proposal (G1j);
- a timeout is never approval, and never strands a ticket (``sweep``): every stage
  gets ONE re-wake of the roles that have not acted, with a fresh window; then no
  proposal returns the ticket to ``proposed``, a reviewer who stayed quiet is
  recorded ``silent`` and the Governor decides with what it has, no decision
  returns the ticket. Only the Governor says ``go`` (staging 2026-10-14: two
  reviews passed by timeout with one message between them).

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
#: wakes carry it; the validator judges only reviews run by this protocol (deploy/railway/phase5_live.py A02/A03)
PROTOCOL = 2
STAGE_WINDOWS = {"proposing": PROPOSAL_WINDOW, "reviewing": REVIEW_WINDOW, "deciding": DECISION_WINDOW}
REVIEW_FOCUS = {"scout": "is there evidence this moves the ticket's key result, and is it worth doing",
                "qa": "can the proof fail on the base revision and pass after the change",
                "security": "what could this change break or expose"}


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


def _log(what: str, review: Dict[str, Any], **facts: Any) -> None:
    """One structural log line per step (ids, roles, stances, verdicts; never proposal or position text),
    so a live review can be followed in the worker log. Written before the caller commits."""
    logger.info("design_review %s review=%s round=%s protocol=%s %s", what, str(review["id"])[:8], review.get("round"), PROTOCOL,
                " ".join(f"{k}={v}" for k, v in facts.items()))


def _ticket_brief(t: Dict[str, Any]) -> Dict[str, Any]:
    return {"ticket_id": str(t["id"]), "proposal_id": str(t["proposal_id"]), "title": t["title"], "objective_id": t["objective_id"],
            "metric_id": t["metric_id"], "direction": t["direction"], "expected_effect": float(t["expected_effect"] or 0), "proof": t["proof"]}


def open_review(db: Session, ticket: Dict[str, Any], *, approved_by: str) -> uuid.UUID:
    """The ticket was approved: open its review (round 1) instead of waking the Architect
    to design. The caller commits."""
    rid = uuid.uuid4()
    db.execute(text("INSERT INTO society_design_reviews (id, ticket_id, plan_id, approved_by, correlation_id) VALUES (:id, :t, :p, :by, :c)"),
               {"id": rid, "t": ticket["id"], "p": ticket.get("plan_id"), "by": approved_by, "c": uuid.uuid4()})
    review = get(db, rid)
    _log("opened", review, ticket=str(ticket["id"])[:8], approved_by=approved_by)
    _request_round(db, review, ticket, feedback=None)
    return rid


#: who acts at each stage, and the event that wakes them (targeted: runs.TARGETED_ONLY_EVENT_TYPES)
STAGE_ROLES = {"proposing": ("architect",), "reviewing": REVIEWERS, "deciding": ("governor",)}
STAGE_EVENTS = {"proposing": EventType.DESIGN_REVIEW_REQUESTED, "reviewing": EventType.DESIGN_REVIEW_PROPOSED, "deciding": EventType.DESIGN_REVIEW_READY}
WAKE_EVENT_TYPES = frozenset(STAGE_EVENTS.values())


def _role_agents(db: Session, roles: Any) -> List[tuple]:
    return [(str(a), str(r)) for a, r in db.execute(text(
        "SELECT g.agent_id, g.role FROM agent_capability_grants g JOIN agents a ON a.id = g.agent_id "
        "WHERE g.enabled AND g.role = ANY(:roles) AND a.status::text IN ('active', 'unverified') ORDER BY g.role, a.name"),
        {"roles": list(roles)}).all()]


def _question(stage: str, role: str, review: Dict[str, Any], title: str, deadline: str) -> str:
    """The one thing this role is asked at this stage (structural: our words, the ticket title)."""
    n = int(review["round"])
    if stage == "proposing":
        return (f"Architect: propose the design for '{title}' (round {n}/{MAX_ROUNDS}): one PROPOSE_DESIGN "
                f"{{review_id, approach, files, proof, risks}} before {deadline}.")
    if stage == "reviewing":
        return (f"{role.capitalize()}: {REVIEW_FOCUS.get(role, 'does this proposal hold up')}? Answer with ONE REVIEW_DESIGN "
                f"{{review_id, stance: support|concern|object|abstain, points}} about THIS proposal before {deadline}; "
                "abstain (the reason as a point) when you have nothing specific -- silence is recorded as silent.")
    return (f"Governor: decide round {n}/{MAX_ROUNDS} of '{title}' with ONE DECIDE_DESIGN {{review_id, verdict: go|revise|drop, "
            f"rationale, conditions}} before {deadline}; a Security object rules out go" + ("; this round has no revise." if n >= MAX_ROUNDS else "."))


def _wake(db: Session, review: Dict[str, Any], *, extra: Optional[Dict[str, Any]] = None, rewake: bool = False, causation: Any = None,
          roles: Optional[List[str]] = None) -> int:
    """Wake the roles that must act at the review's CURRENT stage (or only ``roles`` of them):
    one system event per role agent in the review's story (caused by the contribution that
    moved the stage, if any), targeted so nobody else is woken, each with its question and
    the stage deadline. Returns wakes."""
    stage = review["status"]
    t = _ticket(db, review)
    deadline = (review["stage_at"] + STAGE_WINDOWS[stage]).isoformat(timespec="minutes")
    n = 0
    for agent_id, role in _role_agents(db, roles if roles is not None else STAGE_ROLES[stage]):
        emit_event(db, event_type=STAGE_EVENTS[stage], actor_type="system", subject_type="proposal", subject_id=t["proposal_id"],
                   correlation_id=review["correlation_id"], causation=causation,
                   payload={"review_id": str(review["id"]), "round": int(review["round"]), "stage": stage, "role": role, "target_agent_id": agent_id,
                            "rewake": rewake, "protocol": PROTOCOL, "deadline": deadline, "question": _question(stage, role, review, t["title"], deadline),
                            **_ticket_brief(t), **(extra or {})},
                   idempotency_key=f"design_review.{stage}:{review['id']}:{review['round']}:{agent_id}" + (":rewake" if rewake else ""))
        n += 1
    return n


def _request_round(db: Session, review: Dict[str, Any], ticket: Dict[str, Any], *, feedback: Optional[Dict[str, Any]]) -> None:
    _wake(db, review, extra={"feedback": feedback} if feedback else None)


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
    _log("proposed", review, by="architect", files=len(proposal.get("files") or []))
    _chat(db, review, agent, f"Design proposal (round {review['round']}): {_ticket(db, review)['title']}",
          f"Approach: {proposal['approach']}\nFiles: {', '.join(proposal['files'])}\nProof: {proposal['proof']}\nRisks: {'; '.join(proposal.get('risks') or []) or 'none named'}",
          "proposal")
    _wake(db, review, causation=causation)
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
    _log("position", review, role=role, stance=position["stance"], spoken=len(positions))
    _chat(db, review, agent, f"Review by {role} (round {review['round']}): {position['stance']}", "\n".join(f"- {p}" for p in position["points"]),
          "review_result")
    if all(r in positions for r in REVIEWERS):
        _ready(db, review, why="all reviewers spoke", causation=causation)
    return review


def _missing(review: Dict[str, Any]) -> List[str]:
    return [r for r in REVIEWERS if r not in (review.get("positions") or {})]


def _silent(review: Dict[str, Any]) -> List[str]:
    return [r for r in REVIEWERS if ((review.get("positions") or {}).get(r) or {}).get("stance") == "silent"]


def _governor_brief(review: Dict[str, Any], why: str) -> Dict[str, Any]:
    """Everything the Governor decides on, in its wake: each position (model-authored: untrusted)."""
    from .context import untrusted  # noqa: PLC0415 -- context imports this module

    positions = review.get("positions") or {}
    return {"positions": untrusted({r: {"stance": p["stance"], "points": list(p.get("points") or [])} for r, p in positions.items()}, source="agent:reviewers"),
            "stances": {r: p["stance"] for r, p in positions.items()}, "missing": _missing(review), "silent": _silent(review), "why": why}


def _ready(db: Session, review: Dict[str, Any], *, why: str, causation: Any = None) -> None:
    _update(db, review["id"], status="deciding", stage_at=utcnow())
    _wake(db, get(db, review["id"]), causation=causation, extra=_governor_brief(review, why))


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
            "stances": {r: p["stance"] for r, p in (review.get("positions") or {}).items()}, "missing": _missing(review), "earlier_rounds": len(history)}


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
    _log("decided", review, verdict=verdict, by=decision.get("by"), stances=",".join(f"{r}:{st}" for r, st in sorted(minutes["stances"].items())) or "-")
    _update(db, review["id"], decision=decision, status={"go": "go", "drop": "dropped", "return": "returned"}[verdict], stage_at=utcnow())
    if t["status"] != "approved":
        return  # the ticket moved on (owner, freshness close): the minutes stay on the review
    if verdict == "go":
        tickets.emit_approved(db, t, None, by=review["approved_by"] or "owner", design_review=minutes)
    elif verdict == "drop":
        tickets.set_status(db, t["id"], "refused", f"design review dropped it: {' '.join(str(decision.get('rationale') or '').split())}"[:500])
    else:  # return: the meeting did not decide in time -- the owner/board re-plans it
        tickets.set_status(db, t["id"], "proposed", f"design review: {decision.get('rationale')}"[:500], plan_id=None)


def _rewoken(db: Session, review: Dict[str, Any]) -> bool:
    """Has the CURRENT stage of this round had its one re-wake?"""
    return db.execute(text("SELECT 1 FROM society_events WHERE event_type = :t AND payload ->> 'review_id' = :r AND payload ->> 'round' = :n "
                           "AND payload ->> 'rewake' = 'true' LIMIT 1"),
                      {"t": STAGE_EVENTS[review["status"]], "r": str(review["id"]), "n": str(review["round"])}).first() is not None


def _rewake(db: Session, review: Dict[str, Any], now: datetime, *, roles: Optional[List[str]] = None, extra: Optional[Dict[str, Any]] = None) -> None:
    _update(db, review["id"], stage_at=now)
    _wake(db, get(db, review["id"]), rewake=True, roles=roles, extra=extra)


def _hours(window: timedelta) -> str:
    return f"{window.total_seconds() / 3600:g}h"


def sweep(db: Session, now: datetime) -> int:
    """A timeout is never approval, and no review strands a ticket. The caller commits.
    Returns reviews advanced."""
    n = 0
    rows = db.execute(text("SELECT * FROM society_design_reviews WHERE status IN ('proposing', 'reviewing', 'deciding') ORDER BY created_at FOR UPDATE SKIP LOCKED")).mappings().all()
    for r in rows:
        review = dict(r)
        stage = review["status"]
        if now - review["stage_at"] < STAGE_WINDOWS[stage]:
            continue
        n += 1
        again = not _rewoken(db, review)  # once more, with a fresh window, only those who have not acted
        if stage == "proposing":
            if again:
                _rewake(db, review, now)
            else:
                _settle(db, review, {"verdict": "return", "conditions": [], "by": "runtime",
                                     "rationale": f"no design proposal after a re-wake ({_hours(PROPOSAL_WINDOW)} each): returned for re-planning"})
        elif stage == "reviewing":
            if again:
                _rewake(db, review, now, roles=_missing(review))
            else:
                silent = _missing(review)
                positions = {**(review["positions"] or {}), **{r: {"stance": "silent", "points": [], "agent": None, "by": "runtime"} for r in silent}}
                _update(db, review["id"], positions=positions)
                _ready(db, get(db, review["id"]), why=f"review window ({_hours(REVIEW_WINDOW)}) elapsed after a re-wake; silent: " + ", ".join(silent))
        elif again:
            _rewake(db, review, now, extra=_governor_brief(review, f"no decision within {_hours(DECISION_WINDOW)}: woken once more"))
        else:
            objections = [k for k, p in (review["positions"] or {}).items() if p.get("stance") == "object"]
            _settle(db, review, {"verdict": "return", "conditions": [], "by": "runtime",
                                 "rationale": f"no Governor decision after a re-wake ({_hours(DECISION_WINDOW)} each)" + (f" and {', '.join(objections)} objected" if objections else "")
                                 + ": returned for re-planning"})
    return n


def context_view(db: Session, correlation_id: Any) -> Dict[str, Any]:
    """What a role sees of the review its story belongs to (model-authored parts are untrusted)."""
    review = for_correlation(db, correlation_id)
    if review is None:
        return {}
    t = _ticket(db, review)
    step = {"proposing": "Architect: PROPOSE_DESIGN {review_id, approach, files, proof, risks}",
            "reviewing": "Scout, QA, Security: REVIEW_DESIGN {review_id, stance: support|concern|object|abstain, points} -- once each, about THIS proposal",
            "deciding": "Governor: DECIDE_DESIGN {review_id, verdict: go|revise|drop, rationale, conditions} -- Security object bars go; "
                        f"round {MAX_ROUNDS} has no revise" + (f"; missing: {', '.join(_missing(review))} (decide with what you have)" if _missing(review) else "")
                        + (f"; silent: {', '.join(_silent(review))} (woken twice, said nothing)" if _silent(review) else "")
            }.get(review["status"], "nothing: the review is settled")
    if review["status"] in STAGE_WINDOWS:
        step += f"; deadline {(review['stage_at'] + STAGE_WINDOWS[review['status']]).isoformat(timespec='minutes')}"
    from . import work_packet  # noqa: PLC0415
    from .context import untrusted  # noqa: PLC0415

    return {"design_review": {
        "id": str(review["id"]), "round": int(review["round"]), "max_rounds": MAX_ROUNDS, "status": review["status"], "next": step,
        "ticket": _ticket_brief(t),
        "work_packet": work_packet.for_ticket(db, t),
        "proposal": untrusted(review["proposal"], source="agent:architect") if review["proposal"] else None,
        "positions": untrusted(review["positions"] or {}, source="agent:reviewers"),
        "missing": _missing(review) if review["status"] == "deciding" else [],
        "silent": _silent(review),
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
