"""Company mode: the Chief of Staff's daily plan waits for the owner (tickets.py)."""

from __future__ import annotations

import uuid
from datetime import timedelta

from services.registry.app.models import CodeCandidate, ImprovementProposal, SocietyEvent
from services.registry.app.society import company as company_mod, tickets
from services.registry.app.society.events import EventType, utcnow

from .conftest import auth
from .test_build_engine import SRC, TESTS, _ev
from .test_company_tickets import _request, _run, _status, company as company  # noqa: F401 -- fixture


def _proposed(db, report, *, role="scout", effect=0.02, metric="bench_holdout_pass_at_1", files_tag=""):
    p = ImprovementProposal(id=uuid.uuid4(), proposed_by_agent_id=report.agents[role], source="audit", title=f"t{files_tag}{uuid.uuid4().hex[:6]}",
                            problem="p", proposed_change="c", status="APPROVED", target_scope="platform", importance=60)
    db.add(p)
    db.flush()
    tickets.create(db, proposal_id=p.id, title=p.title, fields={"objective_id": "O3", "metric_id": metric, "expected_effect": effect, "direction": "up",
                   "proof": TESTS + ([f"tests/test_textutil.py::t{files_tag}"] if files_tag else [])}, source="scout", role=role, importance=60)
    db.commit()
    return p


def _activate(api_client, user_token, oid="O3", state="active"):
    _, plain = user_token(None)
    _, op = user_token("operator")
    assert api_client.post(f"/v1/society/company/objectives/{oid}/status", headers=auth(plain), json={"status": state}).status_code == 403
    r = api_client.post(f"/v1/society/company/objectives/{oid}/status", headers=auth(op), json={"status": state})
    assert r.status_code == 200 and {"id": oid, "status": state} in r.json()["objectives"]
    return plain, op


def test_the_plan_ranks_at_most_three_tickets_and_nothing_builds_before_the_owner_approves(db, SessionLocal, company, api_client, user_token):
    report, settings = company
    plain, op = _activate(api_client, user_token)
    scout = [_proposed(db, report, effect=e, files_tag=str(i)) for i, e in enumerate((0.01, 0.05, 0.03))]
    eng = _proposed(db, report, role="architect", effect=0.002, files_tag="e")
    _proposed(db, report, metric="usd_per_objective_pr_30d", files_tag="d")  # wrong direction for its KR: never ranked
    plan = tickets.build_plan(db, None, utcnow())
    db.commit()
    ranked = [r["ticket_id"] for r in plan["ranking"]]
    want = [tickets.for_proposal(db, p.id)["id"] for p in (scout[1], scout[2], eng)]
    assert ranked == [str(t) for t in want], "<= 2 per department, then by owner_priority x impact / cost"
    assert _status(db, scout[0]) == "proposed" and {_status(db, p) for p in (scout[1], scout[2], eng)} == {"planned"}

    row = _run(db, SessionLocal, settings, "architect", [_request(scout[1], tests=TESTS + ["tests/test_textutil.py::t1"])])
    assert _ev(row.execution_status) == "failed" and "only after the owner approves" in row.error
    assert _status(db, scout[1]) == "planned" and db.query(CodeCandidate).count() == 0, "waiting is not a refusal"

    listed = api_client.get("/v1/society/company/plans", headers=auth(op)).json()["plans"]
    assert listed[0]["id"] == plan["id"] and listed[0]["status"] == "awaiting_owner"
    assert api_client.post(f"/v1/society/company/plans/{plan['id']}/approve", headers=auth(plain)).status_code == 403
    assert api_client.post(f"/v1/society/company/plans/{plan['id']}/approve", headers=auth(op)).json()["status"] == "approved"
    assert api_client.post(f"/v1/society/company/plans/{plan['id']}/reject", headers=auth(op)).status_code == 409
    db.expire_all()
    assert {_status(db, p) for p in (scout[1], scout[2], eng)} == {"approved"}
    woke = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.COMPANY_TICKET_APPROVED).all()
    assert {e.subject_id for e in woke} == {scout[1].id, scout[2].id, eng.id} and all(e.actor_type == "operator" for e in woke)

    row = _run(db, SessionLocal, settings, "architect", [_request(scout[1], tests=TESTS + ["tests/test_textutil.py::t1"])])
    assert _ev(row.execution_status) == "executed", row.error
    assert _status(db, scout[1]) == "building" and db.query(CodeCandidate).one().proposal_id == scout[1].id


def test_a_settled_cycle_builds_the_plan_and_a_rejected_plan_closes_its_tickets(db, company, api_client, user_token):
    report, settings = company
    _, op = _activate(api_client, user_token)
    p = _proposed(db, report)
    cycle = company_mod.start_cycle(db, settings, trigger="operator")
    assert company_mod.settle_cycles(db, now=utcnow() + timedelta(hours=1)) == 1
    db.refresh(cycle)
    plan_id = cycle.outcome_detail["plan_id"]
    assert api_client.post(f"/v1/society/company/plans/{plan_id}/reject", headers=auth(op)).json()["status"] == "rejected"
    db.expire_all()
    assert _status(db, p) == "closed"


def test_an_owner_ticket_skips_the_plan_but_never_the_gate(db, SessionLocal, company, api_client, user_token):
    report, settings = company
    _, op = user_token("operator")
    body = {"title": "parse_bool yes", "problem": "yes parses as False", "objective_id": "O3", "metric_id": "bench_holdout_pass_at_1",
            "expected_effect": 0.02, "direction": "up", "proof": TESTS}
    made = api_client.post("/v1/society/company/tickets", headers=auth(op), json=body).json()
    proposal = db.get(ImprovementProposal, uuid.UUID(made["proposal_id"]))
    assert _status(db, proposal) == "approved"
    row = _run(db, SessionLocal, settings, "architect", [_request(proposal)])
    assert _ev(row.execution_status) == "failed" and "objective 'O3' is not active" in row.error and _status(db, proposal) == "refused"
    _activate(api_client, user_token)
    again = api_client.post("/v1/society/company/tickets", headers=auth(op), json=body).json()
    row = _run(db, SessionLocal, settings, "architect", [_request(db.get(ImprovementProposal, uuid.UUID(again["proposal_id"])), files=(SRC,))])
    assert _ev(row.execution_status) == "executed", row.error
