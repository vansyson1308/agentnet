"""Security constraints and rescopes in the design review (design_review.py + spec_gate.py).

Staging 2026-10-26:
- 7ae459d7 passed QA and failed Security for a constraint the design never stated;
- 46b27127 was declined by its Builder (spec_outside_files_allowed) and the ticket died there.

Now a Security ``object`` names the constraint the spec must carry, every later proposal of the
ticket carries it (``carries``), the minutes and the candidate spec keep it; and a Builder's
spec_outside_files_allowed decline re-designs the SAME ticket in a new review that must cover the
blocking paths -- once."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from services.registry.app.maintenance.activities import ScriptedActivityModel
from services.registry.app.models import CodeCandidate, CodeCandidateStatus
from services.registry.app.society import design_review, spec_gate, tickets
from services.registry.app.society.cognition import FakeModel
from services.registry.app.society.events import EventType, utcnow
from services.registry.app.society.worker import SocietyWorker

from .test_builder_decline import _run_as
from .test_company_tickets import company  # noqa: F401 -- fixture
from .test_design_review import ACCEPTANCE, IDLE, _act, _agent, _approved_ticket, _events, _proposing, _review_id, review_on  # noqa: F401 -- fixture

CONSTRAINT = "no change under services/payment or the wallet pages"
SRC = "src/textutil.py"


def _positions(db, report, review, stances, constraints=()):
    for role, stance in stances:
        design_review.post_position(db, review_id=review["id"], correlation_id=review["correlation_id"], agent=_agent(db, report, role), role=role,
                                    position={"stance": stance, "points": [f"{role} point"], "constraints": list(constraints) if role == "security" else []},
                                    causation=None)


def test_a_security_object_names_its_constraint_and_every_later_round_must_carry_it(db, review_on):
    report, _ = review_on
    t, review = _proposing(db, report)
    corr = review["correlation_id"]
    with pytest.raises(design_review.ReviewRefused, match="names the constraint"):
        design_review.post_position(db, review_id=review["id"], correlation_id=corr, agent=_agent(db, report, "security"), role="security",
                                    position={"stance": "object", "points": ["touches money"], "constraints": []}, causation=None)
    with pytest.raises(design_review.ReviewRefused, match="only Security"):
        design_review.post_position(db, review_id=review["id"], correlation_id=corr, agent=_agent(db, report, "scout"), role="scout",
                                    position={"stance": "support", "points": ["fine"], "constraints": ["x"]}, causation=None)
    _positions(db, report, review, (("scout", "support"), ("qa", "support"), ("security", "object")), [CONSTRAINT])
    gov = _agent(db, report, "governor")
    design_review.decide(db, review_id=review["id"], correlation_id=corr, agent=gov, decision={"verdict": "revise", "rationale": "meet the security constraint", "conditions": []})
    review = design_review.get(db, review["id"])
    (round2,) = [e for e in _events(db, EventType.DESIGN_REVIEW_REQUESTED) if e.payload["round"] == 2]
    assert round2.payload["feedback"]["constraints"] == [f"S1: {CONSTRAINT}"]
    ob = design_review.obligations(review)
    assert ob == {"constraints": [f"S1: {CONSTRAINT}"], "required_paths": []}
    (p,) = spec_gate.check_obligations({"files": [SRC], "carries": []}, ob)
    assert "S1 is not carried" in p and CONSTRAINT not in p, "the gate speaks in our words, never the model's"
    assert spec_gate.check_obligations({"files": [SRC], "carries": ["S1: only src/textutil.py changes"]}, ob) == []
    design_review.propose(db, review_id=review["id"], correlation_id=review["correlation_id"], agent=_agent(db, report, "architect"),
                          proposal={"approach": "b" * 30, "files": [SRC], "acceptance_tests": [ACCEPTANCE], "proof": "p" * 10, "risks": [],
                                    "carries": ["S1: only src/textutil.py changes"]}, causation=None)
    review = design_review.get(db, review["id"])
    _positions(db, report, review, (("scout", "support"), ("qa", "support"), ("security", "support")))
    design_review.decide(db, review_id=review["id"], correlation_id=review["correlation_id"], agent=gov, decision={"verdict": "go", "rationale": "the constraint is met", "conditions": []})
    db.commit()
    minutes = design_review.minutes_for_ticket(db, t["id"])
    assert minutes["security_constraints"] == [f"S1: {CONSTRAINT}"]
    (approved,) = _events(db, EventType.COMPANY_TICKET_APPROVED)
    assert approved.payload["design_review"]["security_constraints"] == [f"S1: {CONSTRAINT}"]


def test_the_architect_is_refused_until_the_proposal_carries_the_constraint(db, SessionLocal, review_on):
    report, settings = review_on
    t, review = _proposing(db, report)
    _positions(db, report, review, (("scout", "support"), ("qa", "support"), ("security", "object")), [CONSTRAINT])
    design_review.decide(db, review_id=review["id"], correlation_id=review["correlation_id"], agent=_agent(db, report, "governor"),
                         decision={"verdict": "revise", "rationale": "meet the security constraint", "conditions": []})
    db.commit()
    seen = []

    def decide(ctx):
        data = ctx.event["payload"].get("data") or {}
        if ctx.event["type"] == EventType.DESIGN_REVIEW_REQUESTED and ctx.role == "architect" and data.get("round") == 2:
            seen.append((ctx.event["payload"].get("data") or {}).get("gate"))
            carries = [] if len(seen) == 1 else ["S1: only src/textutil.py changes"]
            return _act({"type": "PROPOSE_DESIGN", "payload": {"review_id": _review_id(ctx), "approach": "extend parse_bool's affirmative set",
                                                               "files": [SRC], "proof": "a failing test first", "acceptance_tests": [ACCEPTANCE], "carries": carries}})
        return IDLE

    worker = SocietyWorker(SessionLocal, settings=settings, model=FakeModel(decide), worker_id="w-carry", telemetry_enabled=False, builder_model=ScriptedActivityModel([]))
    worker.routing = {}
    asyncio.run(worker.run_until_idle(max_cycles=6))
    db.expire_all()
    assert seen[0] is None and any("S1 is not carried" in p for p in seen[1]), seen
    review = design_review.get(db, review["id"])
    assert review["status"] == "reviewing" and review["proposal"]["carries"] == ["S1: only src/textutil.py changes"]


def _declined_ticket(db, report):
    """A ticket whose review said go, and its candidate (requested) that its Builder will decline."""
    t, review = _proposing(db, report)
    _positions(db, report, review, (("scout", "support"), ("qa", "support"), ("security", "concern")), [CONSTRAINT])
    design_review.decide(db, review_id=review["id"], correlation_id=review["correlation_id"], agent=_agent(db, report, "governor"),
                         decision={"verdict": "go", "rationale": "small and provable", "conditions": []})
    cand = CodeCandidate(id=uuid.uuid4(), proposal_id=t["proposal_id"], correlation_id=uuid.uuid4(), requested_by_agent_id=report.agents["architect"],
                         builder_agent_id=report.agents["builder"], title=t["title"], status="requested", risk_tier="green", changed_files=[],
                         spec={"kind": "code", "description": "d" * 20, "expected_effect": "e", "files_allowed": [SRC], "acceptance_tests": [ACCEPTANCE]})
    db.add(cand)
    db.flush()
    tickets.set_status(db, t["id"], "building", candidate_id=cand.id)
    db.commit()
    return t, cand


def _decline(db, SessionLocal, settings, cand, paths=("services/dashboard/app/main.py",)):
    _run_as(db, SessionLocal, settings, "builder", [{"type": "DECLINE_CODE_CANDIDATE", "payload": {
        "candidate_id": str(cand.id), "reason_code": "spec_outside_files_allowed", "detail": "the price needs the view to pass it", "blocking_paths": list(paths)}}])


def test_a_spec_outside_files_allowed_decline_redesigns_the_same_ticket_once(db, SessionLocal, review_on, grants_with_no_cooldown):
    report, settings = review_on
    grants_with_no_cooldown()
    t, cand = _declined_ticket(db, report)
    first = design_review._row(db, "ticket_id = :t", {"t": t["id"]})
    _decline(db, SessionLocal, settings, cand)
    db.expire_all()
    assert db.get(CodeCandidate, cand.id).status == CodeCandidateStatus.REJECTED
    t = tickets.for_proposal(db, t["proposal_id"])
    assert t["status"] == "approved" and t["candidate_id"] is None and "rescope" in t["reason"]
    review = design_review._row(db, "ticket_id = :t", {"t": t["id"]})
    assert review["id"] != first["id"] and review["status"] == "proposing" and review["correlation_id"] != first["correlation_id"]
    (wake,) = [e for e in _events(db, EventType.DESIGN_REVIEW_REQUESTED) if e.payload["review_id"] == str(review["id"])]
    assert wake.payload["feedback"]["rescope"]["blocking_paths"] == ["services/dashboard/app/main.py"]
    ob = design_review.obligations(review)
    assert ob == {"constraints": [f"S1: {CONSTRAINT}"], "required_paths": ["services/dashboard/app/main.py"]}
    assert any("needs services/dashboard/app/main.py" in p for p in spec_gate.check_obligations({"files": [SRC], "carries": ["S1: ok"]}, ob))
    assert spec_gate.check_obligations({"files": [SRC], "unchanged": ["services/dashboard/app/main.py: the view already passes it"], "carries": ["S1: ok"]}, ob) == []
    # while the ticket is being re-designed nobody builds the old design, and the stale-design sweep leaves it alone
    assert design_review.open_for_ticket(db, t["id"])
    assert tickets.sweep_stale_designs(db, utcnow() + timedelta(hours=13)) == 0
    # one rescope per ticket: a second decline closes it as before
    assert design_review.rescope(db, t, blocking_paths=["x.py"], detail="again") is None


def test_a_ticket_without_a_go_review_is_still_closed_by_a_decline(db, SessionLocal, review_on, grants_with_no_cooldown):
    report, settings = review_on
    grants_with_no_cooldown()
    t, cand = _declined_ticket(db, report)
    db.execute(text("UPDATE society_design_reviews SET status = 'returned' WHERE ticket_id = :t"), {"t": t["id"]})
    db.commit()
    _decline(db, SessionLocal, settings, cand)
    assert tickets.for_proposal(db, t["proposal_id"])["status"] == "closed"


def test_a_loop_breaker_trip_is_named_story_reason_swallowed_event_and_root(db, society_settings, engine, caplog):
    """Staging L01 said only 'tripped=1': the audit and the worker log now name the trip."""
    import importlib.util
    import logging
    import pathlib

    from services.registry.app.society.events import emit_event
    from services.registry.app.society.runs import dispatch_pending_events
    from services.registry.app.society.seed import seed_society

    seed_society(db)
    root = emit_event(db, event_type="company.ticket_approved")
    root.status = "processed"
    ev = emit_event(db, event_type=EventType.CODE_CANDIDATE_BUILT, causation=root, subject_type="code_candidate", subject_id=uuid.uuid4())
    ev.causation_depth = society_settings.max_causation_depth + 1
    db.commit()
    with caplog.at_level(logging.WARNING):
        dispatch_pending_events(db, settings=society_settings, routing={})
    db.commit()
    assert any("loop_breaker.tripped" in r.getMessage() and "kind=depth" in r.getMessage() and "code_candidate.built" in r.getMessage() for r in caplog.records)
    spec = importlib.util.spec_from_file_location("phase5_live_lb", pathlib.Path(__file__).resolve().parents[2] / "deploy/railway/phase5_live.py")
    p5 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p5)
    (trip,) = p5.snapshot(engine.raw_connection(), None)["loop_breaker_trips"]
    assert trip["correlation"] == str(ev.correlation_id)[:8] and "causation depth" in trip["note"]
    assert (trip["swallowed"], trip["root"], trip["redelivered"]) == ("code_candidate.built", "company.ticket_approved", False)
    assert trip["subject"].startswith("code_candidate:") and trip["depth"] == society_settings.max_causation_depth + 1
