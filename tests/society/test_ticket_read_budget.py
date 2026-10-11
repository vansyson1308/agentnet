"""One ticket never burns the Architect's hourly cap: a per-ticket read budget, then a
design (REQUEST_CODE_CHANGE) or a structured TICKET_NEEDS_INFO (scripted model, no live claim)."""

from __future__ import annotations

import asyncio
import dataclasses

from sqlalchemy import text

from services.registry.app.maintenance.activities import ScriptedActivityModel
from services.registry.app.models import AgentIntent, AgentRun
from services.registry.app.society import tickets
from services.registry.app.society.cognition import FakeModel
from services.registry.app.society.events import EventType
from services.registry.app.society.worker import SocietyWorker

from .test_company_tickets import _activate, _ticket, company  # noqa: F401 -- fixture
from .test_ticket_design_failures import _approve


def _design_story(db, SessionLocal, settings, decide, cycles=30):
    worker = SocietyWorker(SessionLocal, settings=settings, model=FakeModel(decide), worker_id="w-budget", telemetry_enabled=False,
                           builder_model=ScriptedActivityModel([]))
    worker.routing = {EventType.COMPANY_TICKET_APPROVED: ["architect"], EventType.REPO_READ_RESULT: ["architect"]}
    asyncio.run(worker.run_until_idle(max_cycles=cycles))
    db.expire_all()


def _reads(db, intent_status=None):
    q = db.query(AgentIntent).filter(AgentIntent.intent_type == "SEARCH_REPO")
    return [i for i in q.all() if intent_status is None or i.execution_status.value == intent_status]


def test_reads_past_the_ticket_budget_are_refused_and_count_as_a_failed_design(db, SessionLocal, company):
    report, settings = company
    settings = dataclasses.replace(settings, ticket_read_budget=3)
    _activate(db)
    p = _ticket(db, report)
    seen = []

    def decide(ctx):
        seen.append(ctx.engineering["company"].get("ticket", {}).get("read_budget"))
        return {"decision_summary": "read more", "intents": [{"type": "SEARCH_REPO", "payload": {"pattern": f"parse_bool{len(seen)}"}}], "sleep_for_seconds": 1}

    _approve(db, p)
    _design_story(db, SessionLocal, settings, decide)
    assert len(_reads(db, "executed")) == 3 and len(_reads(db, "failed")) == 1
    assert "ticket read budget (3 reads) is used up" in _reads(db, "failed")[0].error
    assert seen[0]["left"] == 3 and seen[-1]["left"] == 0 and "TICKET_NEEDS_INFO" in seen[-1]["then"]
    runs = db.query(AgentRun).count()
    assert runs <= 5  # the story ends: a refused read emits no repo.read.result to wake on
    t = tickets.for_proposal(db, p.id)
    assert t["status"] == "approved" and t["reason"].startswith("design failed (1/2): read budget (3 reads) spent")


def test_needs_info_returns_only_its_own_ticket_to_the_owner_with_what_is_missing(db, SessionLocal, company):
    report, settings = company
    _activate(db)
    p, other = _ticket(db, report), _ticket(db, report)
    _approve(db, other)
    other_id = str(tickets.for_proposal(db, other.id)["id"])

    def decide(ctx):
        target = other_id  # p's story tries to return ANOTHER story's ticket
        return {"decision_summary": "cannot design", "intents": [{"type": "TICKET_NEEDS_INFO", "payload": {
            "ticket_id": target, "missing": ["target", "proof", "target"], "detail": "the evidence names no failing behaviour to change"}}], "sleep_for_seconds": 1}

    _approve(db, p)
    db.execute(text("UPDATE society_events SET status = 'processed' WHERE event_type = :t AND payload ->> 'ticket_id' = :i"),
               {"t": EventType.COMPANY_TICKET_APPROVED, "i": other_id})
    db.commit()
    _design_story(db, SessionLocal, settings, decide)
    rows = db.query(AgentIntent).filter(AgentIntent.intent_type == "TICKET_NEEDS_INFO").all()
    assert [r.execution_status.value for r in rows] == ["failed"] and "this story designs" in rows[0].error
    assert tickets.for_proposal(db, other.id)["status"] == "approved"
    # its own ticket, from its own story
    def own(ctx):
        t = ctx.engineering["company"]["ticket"]
        return {"decision_summary": "cannot design", "intents": [{"type": "TICKET_NEEDS_INFO", "payload": {
            "ticket_id": t["id"], "missing": ["target", "proof", "target"], "detail": "the evidence names no failing behaviour to change"}}], "sleep_for_seconds": 1}

    p2 = _ticket(db, report)
    _approve(db, p2)
    _design_story(db, SessionLocal, settings, own)
    t = tickets.for_proposal(db, p2.id)
    assert t["status"] == "proposed" and t["plan_id"] is None and t["reason"] == "needs_info [target, proof]: the evidence names no failing behaviour to change"
    assert db.execute(text("SELECT COUNT(*) FROM society_events WHERE event_type = :t"), {"t": EventType.COMPANY_TICKET_NEEDS_INFO}).scalar() == 1
