"""Company mode: every candidate traces to an ACTIVE owner objective (tickets.py)."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import uuid

import pytest
from sqlalchemy import text

from services.registry.app.maintenance.activities import ScriptedActivityModel
from services.registry.app.models import AgentIntent, CodeCandidate, ImprovementProposal
from services.registry.app.society import charter, metrics, tickets
from services.registry.app.society.cognition import _ROLE_RULES, FakeModel
from services.registry.app.society.events import emit_event
from services.registry.app.society.risk import RiskTier, assess
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker

from .test_build_engine import FIX, SRC, SUBMIT, TESTS, _ev

GREEN = "tests/test_textutil.py"  # passes on the fixture's base revision


def test_the_charter_loads_and_refuses_unresolvable_metrics_and_too_many_objectives():
    doc = charter.load()
    assert [o["id"] for o in doc["objectives"]] == ["O1", "O2", "O3"] and all(o["status"] == "proposed" for o in doc["objectives"])
    bad = copy.deepcopy(doc)
    bad["objectives"][0]["key_results"][0]["metric_id"] = "vibes"
    with pytest.raises(charter.CharterError, match="no deterministic source"):
        charter.validate(bad)
    many = copy.deepcopy(doc)
    many["objectives"] = [dict(doc["objectives"][0], id=f"O{i}") for i in range(6)]
    with pytest.raises(charter.CharterError, match="1..5 objectives"):
        charter.validate(many)
    assert assess([charter.CHARTER_REPO_PATH], "").tier == RiskTier.RED


def test_every_key_result_metric_is_a_read_only_aggregate_over_the_real_schema(db):
    assert all(metrics.read(db, m) is None or isinstance(metrics.read(db, m), float) for m in metrics.REGISTRY)


@pytest.fixture
def company(db, code_settings, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    return report, dataclasses.replace(code_settings, company_cycle_enabled=True)


def _activate(db, oid="O3"):
    db.execute(text("INSERT INTO society_objective_status (objective_id, status) VALUES (:o, 'active')"), {"o": oid})
    db.commit()


def _ticket(db, report, *, objective="O3", metric="bench_holdout_pass_at_1", proof=None, ticket=True):
    p = ImprovementProposal(id=uuid.uuid4(), proposed_by_agent_id=report.agents["scout"], source="audit", title=f"parse_bool {uuid.uuid4().hex[:6]}",
                            problem="affirmative words parse as False", proposed_change="extend the set", status="APPROVED", target_scope="platform", importance=60)
    db.add(p)
    db.flush()
    fields = {"objective_id": objective, "metric_id": metric, "expected_effect": 0.02, "direction": "up", "proof": TESTS if proof is None else proof}
    if ticket:
        tickets.create(db, proposal_id=p.id, title=p.title, fields=fields, source="owner", role="scout", importance=60)
    db.commit()
    return p


def _run(db, SessionLocal, settings, role, intents, *, builder_script=None, scripted_rest=False):
    emit_event(db, event_type="t.company", payload={}, correlation_id=uuid.uuid4(), idempotency_key=f"t-company-{uuid.uuid4()}")
    db.commit()
    first = [{"decision_summary": "act", "intents": intents, "sleep_for_seconds": 1}]

    def decide(ctx):
        if ctx.event["type"] == "t.company" and first:
            return first.pop(0)
        if scripted_rest and ctx.role in _ROLE_RULES:
            return _ROLE_RULES[ctx.role](ctx)
        return {"decision_summary": "nothing to do", "intents": [], "sleep_for_seconds": 60}

    worker = SocietyWorker(SessionLocal, settings=settings, model=FakeModel(decide), worker_id="w-company", telemetry_enabled=False,
                           builder_model=ScriptedActivityModel(builder_script or []))
    worker.routing = {**worker.routing, "t.company": [role]} if scripted_rest else {"t.company": [role]}
    asyncio.run(worker.run_until_idle(max_cycles=12 if scripted_rest else 4))
    db.expire_all()
    return db.query(AgentIntent).filter(AgentIntent.intent_type == intents[0]["type"]).order_by(AgentIntent.created_at.desc()).first()


def _request(proposal, files=(SRC,), tests=TESTS):
    return {"type": "REQUEST_CODE_CHANGE", "payload": {"title": "parse_bool accepts yes", "proposal_id": str(proposal.id), "spec": {
        "kind": "code", "description": "parse_bool('yes') is True", "expected_effect": "affirmative task inputs parse", "files_allowed": list(files),
        "acceptance_tests": list(tests)}}}


def _status(db, proposal):
    return tickets.for_proposal(db, proposal.id)["status"]


def test_a_ticket_without_an_active_objective_never_reaches_a_candidate(db, SessionLocal, company):
    report, settings = company
    row = _run(db, SessionLocal, settings, "architect", [_request(_ticket(db, report, ticket=False))])
    assert _ev(row.execution_status) == "failed" and "has no ticket" in row.error
    proposed = _ticket(db, report)  # O3 is only PROPOSED in the charter
    row = _run(db, SessionLocal, settings, "architect", [_request(proposed)])
    assert _ev(row.execution_status) == "failed" and "objective 'O3' is not active" in row.error
    _activate(db)
    wrong_metric = _ticket(db, report, metric="escrow_tasks_completed_7d")
    no_proof = _ticket(db, report, proof=[])
    for p, why in ((wrong_metric, "is not a key result of O3"), (no_proof, "no proof")):
        row = _run(db, SessionLocal, settings, "architect", [_request(p)])
        assert _ev(row.execution_status) == "failed" and why in row.error
    charter_edit = _ticket(db, report)
    row = _run(db, SessionLocal, settings, "architect", [_request(charter_edit, files=(SRC, charter.CHARTER_REPO_PATH))])
    assert _ev(row.execution_status) == "failed" and "owner-edited only" in row.error
    assert db.query(CodeCandidate).count() == 0
    assert {_status(db, p) for p in (proposed, wrong_metric, no_proof, charter_edit)} == {"refused"}


def test_a_meaningful_ticket_is_built_to_ready_with_its_objective_and_a_duplicate_is_refused(db, SessionLocal, company):
    report, settings = company
    _activate(db)
    proposal = _ticket(db, report)
    row = _run(db, SessionLocal, settings, "architect", [_request(proposal)])
    assert _ev(row.execution_status) == "executed", row.error
    cand = db.query(CodeCandidate).one()
    assert cand.spec["expected_effect"].startswith("[O3 bench_holdout_pass_at_1 up 0.02]") and _status(db, proposal) == "building"
    dup = _ticket(db, report)
    row = _run(db, SessionLocal, settings, "architect", [_request(dup)])
    assert _ev(row.execution_status) == "failed" and "duplicate of ticket" in row.error and _status(db, dup) == "refused"
    # the proof fails on base (it is the change to make): freshness lets the build run
    row = _run(db, SessionLocal, settings, "builder", [{"type": "BUILD_CODE_CANDIDATE", "payload": {"candidate_id": str(cand.id)}}],
               builder_script=[FIX, {"action": "run_tests", "args": {}}, SUBMIT], scripted_rest=True)
    assert _ev(row.execution_status) == "executed", row.error
    db.refresh(cand)
    assert _ev(cand.status) == "ready" and _status(db, proposal) == "ready"


def test_a_stale_ticket_whose_proof_already_passes_closes_already_satisfied_without_a_model_call(db, SessionLocal, company):
    report, settings = company
    _activate(db)
    proposal = _ticket(db, report, proof=[GREEN])
    _run(db, SessionLocal, settings, "architect", [_request(proposal, tests=[GREEN])])
    cand = db.query(CodeCandidate).one()
    row = _run(db, SessionLocal, settings, "builder", [{"type": "BUILD_CODE_CANDIDATE", "payload": {"candidate_id": str(cand.id)}}], builder_script=[])
    assert _ev(row.execution_status) == "executed" and row.result["result"]["already_satisfied"] is True
    db.refresh(cand)
    assert _ev(cand.status) == "rejected" and cand.head_sha is None and _status(db, proposal) == "already_satisfied"
