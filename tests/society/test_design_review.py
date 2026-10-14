"""The design review: the company meets before an approved ticket is designed (design_review.py).

The Architect proposes, Scout/QA/Security review, the Governor decides; the protocol,
its rules and its timeouts are deterministic, the models only contribute positions."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from services.registry.app.maintenance.activities import ScriptedActivityModel
from services.registry.app.models import Agent, AgentChat, SocietyEvent
from services.registry.app.society import design_review, tickets
from services.registry.app.society.cognition import FakeModel
from services.registry.app.society.config import reset_settings_cache
from services.registry.app.society.events import EventType, utcnow
from services.registry.app.society.promotion import design_review_lines
from services.registry.app.society.worker import SocietyWorker

from .conftest import auth
from .test_company_tickets import _activate, company  # noqa: F401 -- fixture
from .test_red_tickets import _proposed


@pytest.fixture
def review_on(company, monkeypatch):
    monkeypatch.setenv("SOCIETY_DESIGN_REVIEW_ENABLED", "true")
    reset_settings_cache()
    report, settings = company
    yield report, dataclasses.replace(settings, design_review_enabled=True)
    reset_settings_cache()


def _approved_ticket(db):
    if not db.execute(text("SELECT 1 FROM society_objective_status WHERE objective_id = 'O3'")).first():
        _activate(db)
    t = _proposed(db, effect=0.01, department_role="qa")
    plan = tickets.build_plan(db, None, utcnow())
    db.commit()
    tickets.decide_plan(db, uuid.UUID(plan["id"]), approve=True, user_id=None)
    return tickets.for_proposal(db, t["proposal_id"])


def _events(db, event_type):
    return db.query(SocietyEvent).filter(SocietyEvent.event_type == event_type).order_by(SocietyEvent.created_at).all()


def _agent(db, report, role):
    return db.get(Agent, report.agents[role])


def _review_id(ctx):
    return ctx.engineering["company"]["design_review"]["id"]


def _act(*intents):
    return {"decision_summary": "act", "intents": list(intents), "sleep_for_seconds": 1}


IDLE = {"decision_summary": "nothing to do", "intents": [], "sleep_for_seconds": 60}


def test_an_approved_ticket_meets_before_it_is_designed_and_go_wakes_the_architect_with_the_minutes(db, SessionLocal, review_on):
    report, settings = review_on
    t = _approved_ticket(db)
    assert t["status"] == "approved"
    assert _events(db, EventType.COMPANY_TICKET_APPROVED) == []  # nobody designs before the meeting
    (requested,) = _events(db, EventType.DESIGN_REVIEW_REQUESTED)
    seen = {}

    def decide(ctx):
        etype = ctx.event["type"]
        if etype == EventType.DESIGN_REVIEW_REQUESTED and ctx.role == "architect":
            seen["architect"] = ctx.engineering["company"]["design_review"]
            return _act({"type": "PROPOSE_DESIGN", "payload": {
                "review_id": _review_id(ctx), "approach": "extend parse_bool's affirmative set with yes/on and test both",
                "files": ["src/textutil.py", "tests/test_textutil.py"], "proof": "a new test fails on base and passes after", "risks": ["callers relying on 'yes' -> False"]}})
        if etype == EventType.DESIGN_REVIEW_PROPOSED and ctx.role in ("scout", "qa", "security"):
            stance = "concern" if ctx.role == "qa" else "support"
            return _act({"type": "REVIEW_DESIGN", "payload": {"review_id": _review_id(ctx), "stance": stance, "points": [f"{ctx.role}: the proof is specific"]}})
        if etype == EventType.DESIGN_REVIEW_READY and ctx.role == "governor":
            seen["governor"] = ctx.engineering["company"]["design_review"]
            return _act({"type": "DECIDE_DESIGN", "payload": {"review_id": _review_id(ctx), "verdict": "go", "rationale": "small, provable and nobody objected",
                                                              "conditions": ["keep the change inside textutil"]}})
        if etype == EventType.COMPANY_TICKET_APPROVED and ctx.role == "architect":
            seen["design"] = ctx.event
        return IDLE

    worker = SocietyWorker(SessionLocal, settings=settings, model=FakeModel(decide), worker_id="w-review", telemetry_enabled=False,
                           builder_model=ScriptedActivityModel([]))
    asyncio.run(worker.run_until_idle(max_cycles=12))
    db.expire_all()

    assert seen["architect"]["status"] == "proposing" and seen["architect"]["ticket"]["ticket_id"] == str(t["id"])
    assert set(seen["governor"]["positions"]["data"]) == {"scout", "qa", "security"}
    review = design_review.for_correlation(db, requested.correlation_id)
    assert review["status"] == "go" and review["decision"]["by"] == "Society_Governor"
    (approved,) = _events(db, EventType.COMPANY_TICKET_APPROVED)
    assert approved.correlation_id != requested.correlation_id and approved.causation_depth == 0  # a fresh design story
    minutes = approved.payload["design_review"]
    assert minutes["verdict"] == "go" and minutes["stances"] == {"scout": "support", "qa": "concern", "security": "support"}
    assert minutes["conditions"] == ["keep the change inside textutil"] and "design" in seen
    assert tickets.for_correlation(db, approved.correlation_id)["id"] == t["id"]
    # the meeting is a conversation: proposal, three reviews, the decision, in one thread
    chat = db.query(AgentChat).filter(AgentChat.thread_id == review["id"]).order_by(AgentChat.created_at).all()
    assert [c.title.split(" ")[0] for c in chat] == ["Design", "Review", "Review", "Review", "Decision"]
    assert "Security objected" not in str(review["decision"])
    lines = design_review_lines({"design_review": {**{k: minutes[k] for k in ("review_id", "rounds", "verdict", "by", "stances")}, "conditions": 1}})
    assert lines[0] == "### Design review" and "verdict **go** by Society_Governor" in lines[1] and "qa concern" in lines[2]


def _proposing(db, report):
    t = _approved_ticket(db)
    review = design_review._row(db, "ticket_id = :t", {"t": t["id"]})
    corr = review["correlation_id"]
    design_review.propose(db, review_id=review["id"], correlation_id=corr, agent=_agent(db, report, "architect"),
                          proposal={"approach": "a" * 30, "files": ["src/textutil.py"], "proof": "a failing test", "risks": []}, causation=None)
    return t, design_review.get(db, review["id"])


def test_security_objection_bars_go_the_last_round_has_no_revise_and_drop_refuses_the_ticket(db, review_on):
    report, _ = review_on
    t, review = _proposing(db, report)
    corr = review["correlation_id"]
    with pytest.raises(design_review.ReviewRefused, match="only scout, qa, security"):
        design_review.post_position(db, review_id=review["id"], correlation_id=corr, agent=_agent(db, report, "builder"), role="builder",
                                    position={"stance": "support", "points": ["fine"]}, causation=None)
    for role, stance in (("scout", "support"), ("qa", "support"), ("security", "object")):
        design_review.post_position(db, review_id=review["id"], correlation_id=corr, agent=_agent(db, report, role), role=role,
                                    position={"stance": stance, "points": [f"{role} point"]}, causation=None)
        if role == "scout":
            with pytest.raises(design_review.ReviewRefused, match="scout already reviewed round 1"):
                design_review.post_position(db, review_id=review["id"], correlation_id=corr, agent=_agent(db, report, "scout"), role="scout",
                                            position={"stance": "object", "points": ["again"]}, causation=None)
    gov = _agent(db, report, "governor")
    with pytest.raises(design_review.ReviewRefused, match="Security objected"):
        design_review.decide(db, review_id=review["id"], correlation_id=corr, agent=gov, decision={"verdict": "go", "rationale": "ship it anyway", "conditions": []})
    design_review.decide(db, review_id=review["id"], correlation_id=corr, agent=gov,
                         decision={"verdict": "revise", "rationale": "answer the security objection", "conditions": ["no new dependency"]})
    review = design_review.get(db, review["id"])
    assert (review["round"], review["status"]) == (2, "proposing") and review["correlation_id"] != corr and len(review["history"]) == 1
    (round2,) = [e for e in _events(db, EventType.DESIGN_REVIEW_REQUESTED) if e.payload["round"] == 2]
    assert round2.payload["feedback"]["positions"]["security"]["stance"] == "object"
    with pytest.raises(design_review.ReviewRefused, match="not the review's current round"):
        design_review.propose(db, review_id=review["id"], correlation_id=corr, agent=_agent(db, report, "architect"),
                              proposal={"approach": "b" * 30, "files": ["x"], "proof": "p" * 10, "risks": []}, causation=None)
    corr2 = review["correlation_id"]
    design_review.propose(db, review_id=review["id"], correlation_id=corr2, agent=_agent(db, report, "architect"),
                          proposal={"approach": "b" * 30, "files": ["src/textutil.py"], "proof": "p" * 10, "risks": []}, causation=None)
    for role in design_review.REVIEWERS:
        design_review.post_position(db, review_id=review["id"], correlation_id=corr2, agent=_agent(db, report, role), role=role,
                                    position={"stance": "concern", "points": ["still unclear value"]}, causation=None)
    with pytest.raises(design_review.ReviewRefused, match="the last: go or drop"):
        design_review.decide(db, review_id=review["id"], correlation_id=corr2, agent=gov, decision={"verdict": "revise", "rationale": "once more please", "conditions": []})
    design_review.decide(db, review_id=review["id"], correlation_id=corr2, agent=gov, decision={"verdict": "drop", "rationale": "the value is not worth the risk", "conditions": []})
    db.commit()
    assert design_review.get(db, review["id"])["status"] == "dropped"
    t = tickets.for_proposal(db, t["proposal_id"])
    assert t["status"] == "refused" and "design review dropped it: the value is not worth the risk" in t["reason"]
    assert _events(db, EventType.COMPANY_TICKET_APPROVED) == []


def _wakes(db, review_id):
    """{event type: sorted roles of the runs it created} for one review's stage wakes."""
    rows = db.execute(text("SELECT e.event_type, e.actor_type, e.causation_depth, r.role FROM society_events e JOIN agent_runs r ON r.event_id = e.id "
                           "WHERE e.event_type LIKE 'design_review.%' AND e.payload ->> 'review_id' = :r"), {"r": str(review_id)}).all()
    assert {a for _, a, _, _ in rows} == {"system"} and max(d for _, _, d, _ in rows) <= 2, "every wake is a system event in the review's story"
    out = {}
    for etype, _, _, role in rows:
        out.setdefault(etype, []).append(role)
    return {k: sorted(v) for k, v in out.items()}


def test_each_stage_wakes_exactly_the_roles_that_must_act(db, SessionLocal, review_on):
    """Staging 2026-10-14: two reviews passed with one message between them. Every stage
    change now wakes ITS roles by name (targeted system events), and each gets a run."""
    report, settings = review_on
    t = _approved_ticket(db)
    (requested,) = _events(db, EventType.DESIGN_REVIEW_REQUESTED)
    assert requested.payload["target_agent_id"] == str(report.agents["architect"]) and requested.payload["stage"] == "proposing"

    def decide(ctx):
        etype = ctx.event["type"]
        if etype == EventType.DESIGN_REVIEW_REQUESTED:
            assert ctx.role == "architect"
            return _act({"type": "PROPOSE_DESIGN", "payload": {"review_id": _review_id(ctx), "approach": "a" * 30, "files": ["src/textutil.py"],
                                                               "proof": "a failing test", "risks": []}})
        if etype == EventType.DESIGN_REVIEW_PROPOSED:
            assert ctx.role in design_review.REVIEWERS
            return _act({"type": "REVIEW_DESIGN", "payload": {"review_id": _review_id(ctx), "stance": "support", "points": [f"{ctx.role} agrees"]}})
        if etype == EventType.DESIGN_REVIEW_READY:
            assert ctx.role == "governor"
            return _act({"type": "DECIDE_DESIGN", "payload": {"review_id": _review_id(ctx), "verdict": "go", "rationale": "small and provable", "conditions": []}})
        return IDLE

    worker = SocietyWorker(SessionLocal, settings=settings, model=FakeModel(decide), worker_id="w-wakes", telemetry_enabled=False,
                           builder_model=ScriptedActivityModel([]))
    asyncio.run(worker.run_until_idle(max_cycles=12))
    db.expire_all()
    review = design_review.for_correlation(db, requested.correlation_id)
    assert review["status"] == "go" and review["decision"]["by"] == "Society_Governor"
    assert _wakes(db, review["id"]) == {EventType.DESIGN_REVIEW_REQUESTED: ["architect"], EventType.DESIGN_REVIEW_PROPOSED: ["qa", "scout", "security"],
                                        EventType.DESIGN_REVIEW_READY: ["governor"]}
    roles = db.execute(text("SELECT g.role FROM agent_chat c JOIN agent_capability_grants g ON g.agent_id = c.from_agent_id WHERE c.thread_id = :r"),
                       {"r": review["id"]}).scalars().all()
    assert sorted(roles) == ["architect", "governor", "qa", "scout", "security"]
    assert tickets.for_proposal(db, t["proposal_id"])["status"] == "approved" and len(_events(db, EventType.COMPANY_TICKET_APPROVED)) == 1


def test_a_rate_limited_review_wake_waits_for_its_turn_instead_of_being_dropped(db, SessionLocal, review_on):
    report, settings = review_on
    _approved_ticket(db)
    db.execute(text("UPDATE agent_capability_grants SET max_runs_per_hour = 0 WHERE agent_id = :a"), {"a": report.agents["architect"]})
    db.commit()
    worker = SocietyWorker(SessionLocal, settings=settings, model=FakeModel(lambda ctx: IDLE), worker_id="w-limit", telemetry_enabled=False,
                           builder_model=ScriptedActivityModel([]))
    worker.dispatch()
    (run,) = db.execute(text("SELECT r.id FROM agent_runs r JOIN society_events e ON e.id = r.event_id WHERE e.event_type = :t"),
                        {"t": EventType.DESIGN_REVIEW_REQUESTED}).scalars().all()
    assert asyncio.run(worker.process_run(run)) == "deferred"
    db.expire_all()
    status, error = db.execute(text("SELECT status, error FROM agent_runs WHERE id = :i"), {"i": run}).one()
    assert status == "queued" and "runs/hour limit" in error


def test_a_timeout_is_never_approval(db, review_on):
    """No proposal -> the Architect is woken once more, then the ticket returns to proposed;
    silent reviewers -> the Governor decides with "missing: ..."; no decision -> return."""
    report, _ = review_on
    t1 = _approved_ticket(db)
    r1 = design_review._row(db, "ticket_id = :t", {"t": t1["id"]})
    assert design_review.sweep(db, utcnow() + design_review.PROPOSAL_WINDOW + timedelta(minutes=1)) == 1
    asks = [e for e in _events(db, EventType.DESIGN_REVIEW_REQUESTED) if e.payload["review_id"] == str(r1["id"])]
    assert len(asks) == 2 and asks[1].payload["rewake"] is True and asks[1].payload["target_agent_id"] == str(report.agents["architect"])
    assert design_review.get(db, r1["id"])["status"] == "proposing"
    assert design_review.sweep(db, utcnow() + design_review.PROPOSAL_WINDOW + timedelta(minutes=1)) == 0, "the re-wake gets its own window"
    assert design_review.sweep(db, utcnow() + 2 * design_review.PROPOSAL_WINDOW + timedelta(minutes=2)) == 1
    assert design_review.sweep(db, utcnow() + 3 * design_review.PROPOSAL_WINDOW) == 0, "never a second re-wake"
    db.commit()
    t1 = tickets.for_proposal(db, t1["proposal_id"])
    assert t1["status"] == "proposed" and t1["plan_id"] is None and "no design proposal after a re-wake" in t1["reason"]
    assert design_review.get(db, r1["id"])["status"] == "returned"
    tickets.set_status(db, t1["id"], "refused", "kept out of the next plans of this test")

    # nobody reviewed: the Governor decides anyway, and sees who is missing
    t2, r2 = _proposing(db, report)
    assert design_review.sweep(db, utcnow() + design_review.REVIEW_WINDOW + timedelta(minutes=1)) == 1
    r2 = design_review.get(db, r2["id"])
    assert r2["status"] == "deciding"
    (ready,) = [e for e in _events(db, EventType.DESIGN_REVIEW_READY) if e.payload["review_id"] == str(r2["id"])]
    assert "missing: scout, qa, security" in ready.payload["why"] and ready.payload["target_agent_id"] == str(report.agents["governor"])
    view = design_review.context_view(db, r2["correlation_id"])["design_review"]
    assert view["missing"] == ["scout", "qa", "security"] and "missing: scout, qa, security" in view["next"]
    # ... and no decision in time is a return, never a go
    assert design_review.sweep(db, utcnow() + design_review.DECISION_WINDOW + timedelta(minutes=1)) == 1
    db.commit()
    t2 = tickets.for_proposal(db, t2["proposal_id"])
    assert t2["status"] == "proposed" and "no Governor decision" in t2["reason"]
    tickets.set_status(db, t2["id"], "refused", "kept out of the next plans of this test")

    # one objection, the rest silent: still the Governor's call, still a return on silence
    t3, r3 = _proposing(db, report)
    design_review.post_position(db, review_id=r3["id"], correlation_id=r3["correlation_id"], agent=_agent(db, report, "security"), role="security",
                                position={"stance": "object", "points": ["it widens an auth path"]}, causation=None)
    assert design_review.sweep(db, utcnow() + design_review.REVIEW_WINDOW + timedelta(minutes=1)) == 1
    assert design_review.sweep(db, utcnow() + design_review.DECISION_WINDOW + timedelta(minutes=1)) == 1
    db.commit()
    t3 = tickets.for_proposal(db, t3["proposal_id"])
    assert t3["status"] == "proposed" and "security objected" in t3["reason"]
    assert _events(db, EventType.COMPANY_TICKET_APPROVED) == [], "a timeout never designs a ticket"
    assert not db.execute(text("SELECT 1 FROM society_design_reviews WHERE status = 'go'")).first()


def test_the_validator_counts_runtime_gos_apart_and_fails_when_nobody_decided(db, review_on, engine, capsys):
    from .test_company_board import _phase5

    report, _ = review_on
    p5 = _phase5()
    _, hollow = _proposing(db, report)
    db.execute(text("UPDATE society_design_reviews SET status = 'go', decision = CAST(:d AS JSONB) WHERE id = :i"),
               {"i": hollow["id"], "d": '{"verdict": "go", "by": "runtime", "rationale": "legacy timeout"}'})
    db.commit()
    conn = engine.raw_connection()
    try:
        out = p5.Out()
        p5.step_autonomy(out, conn, 7)
        assert {"autonomy.A02", "autonomy.A03"} <= set(out.failed)
        printed = capsys.readouterr().out
        assert "go by Governor 0, go by runtime 1" in printed and "spoke: architect" in printed
        _, real = _proposing(db, report)
        for role in design_review.REVIEWERS:
            design_review.post_position(db, review_id=real["id"], correlation_id=real["correlation_id"], agent=_agent(db, report, role), role=role,
                                        position={"stance": "support", "points": ["ok"]}, causation=None)
        design_review.decide(db, review_id=real["id"], correlation_id=real["correlation_id"], agent=_agent(db, report, "governor"),
                             decision={"verdict": "go", "rationale": "small and provable", "conditions": []})
        db.commit()
        out = p5.Out()
        p5.step_autonomy(out, conn, 7)
    finally:
        conn.close()
    assert "autonomy.A02" not in out.failed and "autonomy.A03" not in out.failed
    assert "go by Governor 1, go by runtime 1" in capsys.readouterr().out


def test_off_by_default_and_owner_tickets_skip_the_meeting(db, company, review_on):
    report, settings = company
    reset_settings_cache()
    import os

    os.environ.pop("SOCIETY_DESIGN_REVIEW_ENABLED", None)
    reset_settings_cache()
    assert settings.design_review_enabled is False and not design_review.enabled()
    _approved_ticket(db)
    assert len(_events(db, EventType.COMPANY_TICKET_APPROVED)) == 1 and _events(db, EventType.DESIGN_REVIEW_REQUESTED) == []
    os.environ["SOCIETY_DESIGN_REVIEW_ENABLED"] = "true"
    reset_settings_cache()
    tickets.create_owner_ticket(db, user_id=None, title="owner work", problem="the owner asked for it",
                                fields={"objective_id": "O3", "metric_id": "bench_holdout_pass_at_1", "expected_effect": 0.01, "direction": "up",
                                        "proof": ["tests/test_textutil.py"]})
    assert len(_events(db, EventType.COMPANY_TICKET_APPROVED)) == 2 and _events(db, EventType.DESIGN_REVIEW_REQUESTED) == []


def test_the_review_intents_are_low_risk_and_granted_to_the_right_roles():
    from services.registry.app.models import IntentRiskClass
    from services.registry.app.society.policy import RISK_BY_TYPE
    from services.registry.app.society.intents import IntentType
    from services.registry.app.society.roles import DEFAULT_ROLES

    for it in (IntentType.PROPOSE_DESIGN, IntentType.REVIEW_DESIGN, IntentType.DECIDE_DESIGN):
        assert RISK_BY_TYPE[it] == IntentRiskClass.LOW
    holders = {it: {r for r, d in DEFAULT_ROLES.items() if it.value in d.allowed_intents}
               for it in (IntentType.PROPOSE_DESIGN, IntentType.REVIEW_DESIGN, IntentType.DECIDE_DESIGN)}
    assert holders == {IntentType.PROPOSE_DESIGN: {"architect"}, IntentType.REVIEW_DESIGN: {"scout", "qa", "security"},
                       IntentType.DECIDE_DESIGN: {"governor"}}


def test_the_operator_sees_the_minutes(db, api_client, review_on, user_token):
    report, _ = review_on
    _proposing(db, report)
    db.commit()
    _, plain = user_token(None)
    _, op = user_token("operator")
    assert api_client.get("/v1/society/company/reviews", headers=auth(plain)).status_code == 403
    body = api_client.get("/v1/society/company/reviews", headers=auth(op)).json()
    assert body["counts"] == {"reviewing": 1} and body["reviews"][0]["proposal"]["files"] == ["src/textutil.py"]
