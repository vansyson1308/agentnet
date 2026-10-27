"""A ticket design is checked before any build (executor._request_code_change).

Staging 2026-10-14:
- candidate cb82c1a7 named tests/test_agent_sdk_examples.py as its acceptance test. That file
  did not exist on main and was not in files_allowed, so no build could ever pass. It burned
  3 builds before the cap rejected it.
- the Architect's design for bench ticket a6dd4579 carried a ``task_id``. It failed as
  "does not reference an existing task session": a ticket is not a task session.

Both are now refused at REQUEST_CODE_CHANGE with the reason, and the Architect gets ONE
corrective turn in the same story. A ticket story offers only the design intents."""

from __future__ import annotations

import uuid

from sqlalchemy import text

from services.registry.app.models import Agent, AgentCapabilityGrant, AgentRun, CodeCandidate, SocietyEvent
from services.registry.app.society import tickets
from services.registry.app.society.context import TICKET_STORY_INTENTS, build_context
from services.registry.app.society.events import EventType
from services.registry.app.society.intents import REPO_READ_INTENT_TYPES

from .test_build_engine import SRC, TESTS, _ev
from .test_company_tickets import _activate, _request, _run, _ticket, company  # noqa: F401 -- fixture

#: candidate cb82c1a7's spec, as the Architect wrote it
CB82_FILES = ["examples/agent_sdk.py", "services/dashboard/app/templates/create_offer.html"]
CB82_TESTS = ["tests/test_agent_sdk_examples.py"]


def _corrective(db):
    return db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.CODE_CHANGE_SPEC_REJECTED).all()


def test_an_acceptance_file_that_is_neither_on_base_nor_allowed_is_refused_before_any_build(db, SessionLocal, company):
    report, settings = company
    _activate(db)
    proposal = _ticket(db, report)
    tickets._approved(db, tickets.for_proposal(db, proposal.id), None)  # as the owner's or the board's approval does
    db.commit()
    row = _run(db, SessionLocal, settings, "architect", [_request(proposal, files=CB82_FILES, tests=CB82_TESTS)])
    assert _ev(row.execution_status) == "failed" and row.error == "acceptance file refused (acceptance_file_missing): tests/test_agent_sdk_examples.py"
    assert db.query(CodeCandidate).count() == 0, "no candidate, so no build is ever spent on it"
    # one corrective turn, addressed to the Architect, in the same story -- and it got a run
    (ev,) = _corrective(db)
    assert ev.correlation_id == db.get(AgentRun, row.run_id).correlation_id and ev.payload["target_agent_id"] == str(report.agents["architect"])
    assert ev.payload["errors"] == [{"field": "acceptance_tests", "code": "acceptance_file_missing", "path": "tests/test_agent_sdk_examples.py",
                                     "expected": "a test file that exists on the base, or a test_first file of the approved design (the Architect writes it)"}]
    assert db.execute(text("SELECT role FROM agent_runs WHERE event_id = :e"), {"e": ev.id}).scalars().all() == ["architect"]
    t = tickets.for_proposal(db, proposal.id)  # counted as a failed design attempt: the corrective turn is the second
    assert t["status"] == "approved" and t["reason"] == "design failed (1/2): acceptance file refused (acceptance_file_missing): tests/test_agent_sdk_examples.py"


def test_a_new_test_the_builder_would_write_is_refused_and_a_test_on_base_needs_no_listing(db, SessionLocal, company):
    """Staging 2026-10-26 (ffe44239): a test in files_allowed that judges the change is one QA always
    fails (no_self_judging). A new acceptance test is the Architect's test_first (spec_gate.py)."""
    report, settings = company
    _activate(db)
    new_test = _run(db, SessionLocal, settings, "architect", [_request(_ticket(db, report), files=CB82_FILES + CB82_TESTS, tests=CB82_TESTS)])
    assert _ev(new_test.execution_status) == "failed" and new_test.error == "acceptance file refused (acceptance_self_judging): tests/test_agent_sdk_examples.py"
    on_base = _run(db, SessionLocal, settings, "architect", [_request(_ticket(db, report), files=(SRC,), tests=TESTS)])
    assert _ev(on_base.execution_status) == "executed", on_base.error


def test_a_ticket_design_names_no_task_id(db, SessionLocal, company):
    report, settings = company
    _activate(db)
    proposal = _ticket(db, report)
    ticket_id = tickets.for_proposal(db, proposal.id)["id"]
    req = _request(proposal)
    req["payload"]["task_id"] = str(ticket_id)  # what a6dd4579's design most likely sent: the ticket's own id
    row = _run(db, SessionLocal, settings, "architect", [req])
    assert _ev(row.execution_status) == "failed" and "a ticket's design names no task_id" in row.error
    assert db.query(CodeCandidate).count() == 0 and len(_corrective(db)) == 1


def test_a_ticket_story_offers_the_architect_only_the_design_intents(db, company):
    report, settings = company
    _activate(db)
    proposal = _ticket(db, report)
    t = tickets.for_proposal(db, proposal.id)
    tickets.emit_approved(db, t, None, by="board")
    db.commit()
    grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.role == "architect").one()
    agent = db.get(Agent, grant.agent_id)
    story = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.COMPANY_TICKET_APPROVED).one()
    offered = build_context(db, agent=agent, grant=grant, event=story, run=None, settings=settings).permissions
    assert set(offered["allowed_intents"]) == set(TICKET_STORY_INTENTS) | {x.value for x in REPO_READ_INTENT_TYPES}
    assert "CREATE_TASK" in grant.allowed_intents and "CREATE_TASK" not in offered["allowed_intents"] and "ticket story" in offered["story"]
    # any other story keeps the grant's full list
    other = SocietyEvent(id=uuid.uuid4(), event_type="t.other", actor_type="system", payload={}, correlation_id=uuid.uuid4(), idempotency_key=f"t-{uuid.uuid4()}")
    db.add(other)
    db.commit()
    plain = build_context(db, agent=agent, grant=grant, event=other, run=None, settings=settings).permissions
    assert "CREATE_TASK" in plain["allowed_intents"] and "story" not in plain


def test_an_intent_a_ticket_story_does_not_offer_is_denied(db, SessionLocal, company):
    report, settings = company
    _activate(db)
    proposal = _ticket(db, report)
    tickets.emit_approved(db, tickets.for_proposal(db, proposal.id), None, by="board")
    db.commit()
    import asyncio

    from services.registry.app.maintenance.activities import ScriptedActivityModel
    from services.registry.app.models import AgentIntent
    from services.registry.app.society.cognition import FakeModel
    from services.registry.app.society.worker import SocietyWorker

    def decide(ctx):
        if ctx.event["type"] == EventType.COMPANY_TICKET_APPROVED:
            return {"decision_summary": "x", "sleep_for_seconds": 1, "intents": [
                {"type": "UPDATE_GOAL", "payload": {"goal_id": str(uuid.uuid4()), "status": "completed"}}]}
        return {"decision_summary": "idle", "intents": [], "sleep_for_seconds": 60}

    worker = SocietyWorker(SessionLocal, settings=settings, model=FakeModel(decide), worker_id="w-story", telemetry_enabled=False,
                           builder_model=ScriptedActivityModel([]))
    asyncio.run(worker.run_until_idle(max_cycles=4))
    db.expire_all()
    row = db.query(AgentIntent).filter(AgentIntent.intent_type == "UPDATE_GOAL").one()
    assert _ev(row.execution_status) == "denied" and "not offered in this story (ticket story" in row.policy_reason
