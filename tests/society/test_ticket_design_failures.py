"""An approved ticket is never silently stuck: WHY a design failed rides the ticket, and
the second failure returns it to proposed with the reason (tickets.record_design_failure)."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import text

from services.registry.app.models import CodeCandidate
from services.registry.app.society import charter, tickets
from services.registry.app.society.events import EventType, utcnow

from .test_build_engine import SRC
from .test_company_tickets import _activate, _request, _run, _ticket, company  # noqa: F401 -- fixture


def _approve(db, proposal, plan_id=None):
    t = tickets.for_proposal(db, proposal.id)
    if plan_id:
        db.execute(text("UPDATE society_tickets SET plan_id = :p WHERE id = :i"), {"p": plan_id, "i": t["id"]})
    tickets._approved(db, tickets.for_proposal(db, proposal.id), None)
    db.commit()
    return tickets.for_proposal(db, proposal.id)


def _events(db, kind, ticket_id):
    return db.execute(text("SELECT COUNT(*) FROM society_events WHERE event_type = :t AND payload ->> 'ticket_id' = :i"), {"t": kind, "i": str(ticket_id)}).scalar()


def test_a_failed_design_is_on_the_ticket_and_the_second_returns_it_to_proposed(db, SessionLocal, company):
    report, settings = company
    _activate(db)
    p = _ticket(db, report)
    t = _approve(db, p, plan_id=uuid.uuid4())
    row = _run(db, SessionLocal, settings, "architect", [_request(p, tests=[])])
    assert row.execution_status.value == "failed" and "must name acceptance tests" in row.error
    db.expire_all()
    t = tickets.for_proposal(db, p.id)
    assert t["status"] == "approved" and t["reason"].startswith("design failed (1/2): a code change must name acceptance tests")
    # a ticket id passed as proposal_id still lands on the ticket
    bad = {**_request(p, tests=[]), "payload": {**_request(p, tests=[])["payload"], "proposal_id": str(t["id"])}}
    row = _run(db, SessionLocal, settings, "architect", [bad])
    assert row.execution_status.value == "failed"
    db.expire_all()
    t = tickets.for_proposal(db, p.id)
    assert t["status"] == "proposed" and t["plan_id"] is None and t["reason"].startswith("refused 2x by REQUEST_CODE_CHANGE, back to proposed:")
    assert _events(db, EventType.COMPANY_TICKET_DESIGN_FAILED, t["id"]) == 2 and _events(db, EventType.COMPANY_TICKET_RETURNED, t["id"]) == 1
    # the owner re-approves it in a NEW plan: a fresh wake and a fresh count
    t = _approve(db, p, plan_id=uuid.uuid4())
    assert _events(db, EventType.COMPANY_TICKET_APPROVED, t["id"]) == 2
    row = _run(db, SessionLocal, settings, "architect", [_request(p)])
    assert row.execution_status.value == "executed", row.error
    assert db.query(CodeCandidate).count() == 1 and tickets.for_proposal(db, p.id)["status"] == "building"


def test_a_gate_refusal_closes_the_ticket_refused_and_is_not_a_design_attempt(db, SessionLocal, company):
    report, settings = company
    _activate(db)
    p = _ticket(db, report)
    _approve(db, p)
    row = _run(db, SessionLocal, settings, "architect", [_request(p, files=(SRC, charter.CHARTER_REPO_PATH))])
    assert row.execution_status.value == "failed" and "owner-edited only" in row.error
    db.expire_all()
    t = tickets.for_proposal(db, p.id)
    assert t["status"] == "refused" and "owner-edited only" in t["reason"] and _events(db, EventType.COMPANY_TICKET_DESIGN_FAILED, t["id"]) == 0


def test_an_approved_ticket_nobody_designs_counts_one_attempt_per_quiet_period(db, company):
    report, _ = company
    _activate(db)
    p = _ticket(db, report)
    t = _approve(db, p)
    now = utcnow()
    assert tickets.sweep_stale_designs(db, now + timedelta(hours=1)) == 0
    assert tickets.sweep_stale_designs(db, now + timedelta(hours=7)) == 1
    db.commit()
    assert tickets.sweep_stale_designs(db, now + timedelta(hours=8)) == 0  # the same quiet period is counted once
    assert tickets.for_proposal(db, p.id)["reason"].startswith("design failed (1/2): no REQUEST_CODE_CHANGE within 6h")
    assert _events(db, EventType.COMPANY_TICKET_DESIGN_FAILED, t["id"]) == 1
    tickets.sweep_stale_designs(db, now + timedelta(hours=13))
    db.commit()
    t = tickets.for_proposal(db, p.id)
    assert t["status"] == "proposed" and "no REQUEST_CODE_CHANGE within 12h" in t["reason"]


def test_a_capacity_refusal_is_waiting_not_a_design_failure(db, company):
    report, _ = company
    _activate(db)
    p = _ticket(db, report)
    t = _approve(db, p)
    assert tickets.record_design_failure(db, p.id, "portfolio full: 1 open high-risk investigation(s); finish it first", key="i1") == "approved"
    assert tickets.record_design_failure(db, p.id, "change budget exhausted: 2 RED candidates today", key="i2") == "approved"
    db.commit()
    t = tickets.for_proposal(db, p.id)
    assert t["status"] == "approved" and t["reason"].startswith("waiting: change budget") and _events(db, EventType.COMPANY_TICKET_DESIGN_FAILED, t["id"]) == 0
