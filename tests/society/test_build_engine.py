"""One engine: the Society Builder builds a code candidate through the
maintenance coding harness (BUILD_CODE_CANDIDATE -> engineering/build_engine).

The activity model here is scripted (never live evidence); what is proven is
the wiring: the harness works the candidate worktree scoped to the spec, a red
worktree is never submitted, the committed head goes through the unchanged
QA/Security path to READY, a failed build hands the candidate back with its
cost recorded, and a live Builder can no longer write a code candidate blind.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from decimal import Decimal

from services.registry.app.maintenance.activities import ScriptedActivityModel
from services.registry.app.models import AgentIntent, AgentRun, CodeCandidate, ImprovementProposal
from services.registry.app.society.cognition import _ROLE_RULES, FakeModel
from services.registry.app.society.events import emit_event
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker

SRC = "app/textutil.py"
TESTS = ["tests/acceptance/test_parse_bool_regression.py::test_parse_bool_accepts_affirmative_words",
         "tests/acceptance/test_parse_bool_regression.py::test_parse_bool_still_rejects_negatives"]
FIX = {"action": "apply_patch", "args": {"files": [{"path": SRC, "operations": [
    {"op": "replace_exact", "old": '_TRUE = {"true", "1"}', "new": '_TRUE = {"true", "1", "yes", "on", "y"}'}]}]}}
RED = {"action": "apply_patch", "args": {"files": [{"path": SRC, "operations": [
    {"op": "replace_exact", "old": '_TRUE = {"true", "1"}', "new": '_TRUE = {"true", "1", "yes"}'}]}]}}
SUBMIT = {"action": "submit", "result": {"summary": "parse_bool accepts yes/on/y"}}


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _candidate(db, report, *, kind="code"):
    proposal = ImprovementProposal(id=uuid.uuid4(), proposed_by_agent_id=report.agents["scout"], source="audit", title="parse_bool rejects yes/on",
                                   problem="affirmative words parse as False", proposed_change="extend the affirmative set", status="CONVERTED_TO_TASK",
                                   target_scope="platform", importance=60)
    db.add(proposal)
    db.flush()
    cand = CodeCandidate(id=uuid.uuid4(), proposal_id=proposal.id, correlation_id=uuid.uuid4(), requested_by_agent_id=report.agents["architect"],
                         title="parse_bool accepts affirmative words", status="requested", risk_tier="green",
                         spec={"kind": kind, "description": "parse_bool('yes'/'on'/'y') must be True; negatives unchanged",
                               "expected_effect": "task inputs with yes/on parse as True", "files_allowed": [SRC], "acceptance_tests": list(TESTS)})
    db.add(cand)
    db.commit()
    return cand


def _build(db, SessionLocal, settings, cand, script, *, intent=None, then_scripted_roles=False):
    """One Builder decision on a test event; with ``then_scripted_roles`` the
    rest of the society (QA, Security, ...) runs its ordinary scripted rules."""
    emit_event(db, event_type="t.build", payload={}, correlation_id=cand.correlation_id, idempotency_key=f"t-build-{uuid.uuid4()}")
    db.commit()
    first = [{"decision_summary": "build it", "intents": [intent or {"type": "BUILD_CODE_CANDIDATE", "payload": {"candidate_id": str(cand.id)}}], "sleep_for_seconds": 1}]

    def decide(context):
        if context.event["type"] == "t.build" and first:
            return first.pop(0)
        if then_scripted_roles and context.role in _ROLE_RULES:
            return _ROLE_RULES[context.role](context)
        return {"decision_summary": "nothing to do", "intents": [], "sleep_for_seconds": 60}

    worker = SocietyWorker(SessionLocal, settings=settings, model=FakeModel(decide), worker_id="w-build", telemetry_enabled=False,
                           builder_model=ScriptedActivityModel(script))
    worker.routing = {**worker.routing, "t.build": ["builder"]} if then_scripted_roles else {"t.build": ["builder"]}
    asyncio.run(worker.run_until_idle(max_cycles=12 if then_scripted_roles else 4))
    db.expire_all()
    return db.query(AgentIntent).filter(AgentIntent.intent_type == (intent or {}).get("type", "BUILD_CODE_CANDIDATE")).order_by(AgentIntent.created_at.desc()).first()


def test_the_builder_builds_through_the_harness_and_the_head_reaches_ready_through_unchanged_qa(db, SessionLocal, code_settings, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    cand = _candidate(db, report)
    row = _build(db, SessionLocal, code_settings, cand, [FIX, {"action": "run_tests", "args": {}}, SUBMIT], then_scripted_roles=True)
    assert _ev(row.execution_status) == "executed", row.error
    engine = row.result["result"]["engine"]
    assert engine["ok"] and engine["test_runs"] == 1 and engine["patches"] == 1 and engine["actions"] == ["apply_patch", "run_tests", "submit"]
    run = db.get(AgentRun, row.run_id)
    assert Decimal(str(run.cost_usd)) >= Decimal("0.0006"), "the engine's model cost is the run's cost"
    # QA and Security ran as the ordinary scripted roles, unchanged
    db.refresh(cand)
    assert cand.changed_files == [SRC] and cand.builder_agent_id == report.agents["builder"]
    assert _ev(cand.status) == "ready", (cand.status, cand.qa_report)
    assert cand.qa_report["verdict"] == "pass" and cand.qa_report["head_sha"] == cand.head_sha


def test_a_red_worktree_is_never_submitted_and_the_candidate_is_handed_back_with_its_cost(db, SessionLocal, code_settings, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    cand = _candidate(db, report)
    row = _build(db, SessionLocal, code_settings, cand, [RED] + [SUBMIT] * 20)
    assert _ev(row.execution_status) == "failed" and "build failed" in row.error and "BUILD again or DECLINE_CODE_CANDIDATE" in row.error
    db.refresh(cand)
    assert _ev(cand.status) == "requested" and cand.head_sha is None and "build failed" in cand.error
    assert Decimal(str(db.get(AgentRun, row.run_id).cost_usd)) > 0, "a failed build still costs what it cost"


def test_a_rescope_answer_names_the_files_and_points_at_decline(db, SessionLocal, code_settings, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    cand = _candidate(db, report)
    rescope = {"action": "needs_rescope", "result": {"reason": "file_outside_scope", "required_files": ["app/other.py"], "evidence": "the parser lives elsewhere"}}
    row = _build(db, SessionLocal, code_settings, cand, [rescope])
    assert _ev(row.execution_status) == "failed" and "['app/other.py']" in row.error and "DECLINE_CODE_CANDIDATE" in row.error
    db.refresh(cand)
    assert _ev(cand.status) == "requested"


def test_a_live_builder_cannot_write_a_code_candidate_blind_and_docs_are_not_built_by_the_harness(db, SessionLocal, code_settings, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    cand = _candidate(db, report)
    live = dataclasses.replace(code_settings, model_provider="openai_compatible")
    edits = {"type": "SUBMIT_CODE_CANDIDATE", "payload": {"candidate_id": str(cand.id), "edits": [{"path": SRC, "content": "x = 1\n"}], "summary": "blind rewrite"}}
    row = _build(db, SessionLocal, live, cand, [], intent=edits)
    assert _ev(row.execution_status) == "failed" and "BUILD_CODE_CANDIDATE" in row.error
    db.refresh(cand)
    assert _ev(cand.status) == "requested" and cand.head_sha is None
    docs = _candidate(db, report, kind="docs")
    row = _build(db, SessionLocal, code_settings, docs, [FIX, SUBMIT])
    assert _ev(row.execution_status) == "failed" and "docs candidate" in row.error


def test_without_a_live_model_the_build_is_refused_not_faked(db, SessionLocal, code_settings, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    cand = _candidate(db, report)
    ev = emit_event(db, event_type="t.build", payload={}, correlation_id=cand.correlation_id, idempotency_key=f"t-build-{uuid.uuid4()}")
    db.commit()
    decision = {"decision_summary": "build", "intents": [{"type": "BUILD_CODE_CANDIDATE", "payload": {"candidate_id": str(cand.id)}}], "sleep_for_seconds": 1}
    worker = SocietyWorker(SessionLocal, settings=code_settings, model=FakeModel({"builder": [decision]}), worker_id="w-nomodel", telemetry_enabled=False)
    worker.routing = {"t.build": ["builder"]}
    asyncio.run(worker.run_until_idle(max_cycles=4))
    db.expire_all()
    row = db.query(AgentIntent).filter(AgentIntent.intent_type == "BUILD_CODE_CANDIDATE").one()
    assert _ev(row.execution_status) == "failed" and "no live builder model" in row.error and ev is not None
    db.refresh(cand)
    assert _ev(cand.status) == "requested" and cand.builder_agent_id is None
