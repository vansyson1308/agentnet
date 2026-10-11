"""Staging QC 2026-10-11: bench tickets whose task the harness already delivers stayed
proposed / got approved (their proof could never beat a 3/3 baseline), and approved
tickets read "refused 2x" when the society worker was the one that died (tickets.py)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from services.registry.app.models import AgentRun, AgentRunStatus, SocietyEvent
from services.registry.app.society import tickets
from services.registry.app.society.events import EventType, utcnow

from .test_company_tickets import _activate, _request, _run, _ticket, company  # noqa: F401 -- fixture
from .test_ticket_design_failures import _approve, _events
from .test_ticket_work_packet import TASK, _bench_ticket, _report


def _runs(delivered):
    return {"split": "dev", "delivered": delivered, "runs": ["pass"] * delivered + ["tests_fail"] * (3 - delivered)}


@pytest.mark.parametrize("delivered, closed", [(3, True), (2, True), (1, False)])
def test_a_bench_ticket_already_delivered_two_of_three_closes_and_is_never_planned(db, company, monkeypatch, delivered, closed):
    report, _ = company
    _activate(db)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "r-running")
    _report(db, {TASK: _runs(0)}, revision="r-old")
    _report(db, {TASK: _runs(delivered)}, revision="r-running")
    _report(db, {TASK: _runs(0)}, revision="r-newer-elsewhere")  # newer, but not the running revision
    p, tid = _bench_ticket(db, report)
    db.execute(text("UPDATE society_tickets SET status = 'proposed', source = 'backlog' WHERE id = :i"), {"i": tid})
    db.commit()
    plan = tickets.build_plan(db, None, utcnow())
    db.commit()
    t = tickets.for_proposal(db, p.id)
    if closed:
        assert plan["id"] is None and t["status"] == "already_satisfied", "never put in a plan"
        assert t["reason"].startswith(f"bench:{TASK} already delivered {delivered}/3 (>= 2/3) on revision r-running")
    else:
        assert [r["ticket_id"] for r in plan["ranking"]] == [str(tid)] and t["status"] == "planned"


def test_an_approved_satisfied_bench_ticket_closes_at_the_sweep_and_at_the_gate(db, SessionLocal, company, monkeypatch):
    report, settings = company
    _activate(db)
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    _report(db, {TASK: _runs(1)})
    swept, _ = _bench_ticket(db, report)
    gated, _ = _bench_ticket(db, report)
    assert tickets.close_satisfied(db) == 0
    _report(db, {TASK: _runs(3)})  # main's newest report: the task is delivered now
    db.execute(text("UPDATE society_tickets SET status = 'building' WHERE proposal_id = :p"), {"p": gated.id})  # the gate's view
    assert tickets.close_satisfied(db) == 1
    db.commit()
    assert tickets.for_proposal(db, swept.id)["status"] == "already_satisfied"
    db.execute(text("UPDATE society_tickets SET status = 'approved' WHERE proposal_id = :p"), {"p": gated.id})
    db.commit()
    row = _run(db, SessionLocal, settings, "architect", [_request(gated, files=("services/registry/app/maintenance/harness.py",), tests=())])
    assert row.execution_status.value == "failed" and "ticket already satisfied" in row.error
    db.expire_all()
    t = tickets.for_proposal(db, gated.id)
    assert t["status"] == "already_satisfied" and _events(db, EventType.COMPANY_TICKET_DESIGN_FAILED, t["id"]) == 0


def test_a_quiet_period_with_dead_worker_runs_waits_and_is_not_a_design_refusal(db, company):
    report, _ = company
    _activate(db)
    p = _ticket(db, report)
    t = _approve(db, p)
    approved = db.execute(text("SELECT MAX(created_at) FROM society_events WHERE event_type = :t"), {"t": EventType.COMPANY_TICKET_APPROVED}).scalar()
    ev = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.COMPANY_TICKET_APPROVED).first()
    db.add(AgentRun(id=uuid.uuid4(), agent_id=report.agents["scout"], event_id=ev.id, role="scout", status=AgentRunStatus.DEAD, correlation_id=uuid.uuid4(),
                    context_summary={}, intents_count=0, cost_usd=0, attempt=3, error="worker error: IndexError: 4", completed_at=approved + timedelta(hours=2)))
    db.commit()
    assert tickets.sweep_stale_designs(db, approved + timedelta(hours=7)) == 1
    db.commit()
    t = tickets.for_proposal(db, p.id)
    assert t["status"] == "approved" and _events(db, EventType.COMPANY_TICKET_DESIGN_FAILED, t["id"]) == 0
    assert t["reason"] == ("waiting: no REQUEST_CODE_CHANGE within 6h of approval: the society worker had 1 DEAD run(s) then "
                           "(worker error: IndexError: 4), not a design refusal")
    tickets.sweep_stale_designs(db, approved + timedelta(hours=13))  # a healthy quiet period counts
    db.commit()
    t = tickets.for_proposal(db, p.id)
    assert t["reason"].startswith("design failed (1/2): no REQUEST_CODE_CHANGE within 12h") and _events(db, EventType.COMPANY_TICKET_DESIGN_FAILED, t["id"]) == 1
