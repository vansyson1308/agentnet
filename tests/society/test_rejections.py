"""Rejected candidates grouped by ONE structured reason (society/rejections.py), and the
design-review payload limits (owner 2026-10-29: 23 rejected, 27/191 invalid intents in 48h)."""

from __future__ import annotations

import importlib.util
import inspect
import pathlib
import uuid
from datetime import timedelta

import pytest

from services.registry.app.models import AgentIntent, AgentRun, CodeCandidate
from services.registry.app.society import boardroom, daily_report, rejections
from services.registry.app.society.events import EventType, emit_event, utcnow
from services.registry.app.society.intents import (
    CONDITIONS_LIMIT,
    POINTS_LIMIT,
    AgentDecision,
    IntentType,
    normalize_payload,
    validate_intents,
)
from services.registry.app.society.seed import seed_society


def _p5():
    spec = importlib.util.spec_from_file_location("phase5_live_rej", pathlib.Path(__file__).resolve().parents[2] / "deploy/railway/phase5_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CASES = [
    ({}, [], None, "closed_by_owner: pull request closed without merge (PR #115)", "closed_by_owner"),
    ({"declined": True, "reason_code": "acceptance_unsatisfiable"}, [], None, None, "declined"),
    ({"declined": True, "reason_code": "spec_outside_files_allowed"}, [], None, None, "outside_files_allowed"),
    ({"build_attempts_exhausted": 3}, [], None, None, "build_attempts_exhausted"),
    ({"already_satisfied": True}, [], None, None, "already_satisfied"),
    ({"red_on_base": []}, [], None, None, "red_on_base"),
    ({"test_first_green": ["tests/test_x.py"]}, [], None, None, "test_first_red"),
    ({"busywork": True}, [], None, None, "busywork"),
    (None, ["no_self_judging: edits its own test"], "fail", None, "security"),
    (None, ["no_self_judging: edits its own test", "allow_list: x"], None, None, "no_self_judging"),
    (None, ["allow_list: touched b.py"], None, None, "outside_files_allowed"),
    (None, ["test_first_red_on_base: passes on base"], None, None, "test_first_red"),
    (None, ["test_first_green_on_head: still red"], None, None, "test_first_green"),
    (None, ["ci_collects_new_tests: tests/x/test_y.py"], None, None, "ci_orphan"),
    (None, ["test_first_intact: edited"], None, None, "test_first_tampered"),
    (None, ["acceptance_tests: 1 failed"], None, None, "acceptance_failed"),
    (None, ["lint: E9"], None, None, "qa_other"),
    (None, None, None, "stalled", "other"),
]


@pytest.mark.parametrize("event,failures,verdict,error,expected", CASES)
def test_each_rejection_has_one_reason_and_the_validator_agrees(event, failures, verdict, error, expected):
    assert rejections.classify(event, failures, verdict, error) == expected
    assert _p5().classify_rejection(event, failures, verdict, error) == expected
    assert expected in rejections.REASONS


def test_the_validator_copy_is_verbatim():
    p5 = _p5()
    assert p5.QA_CHECKS == rejections.QA_CHECKS and p5.SINCE_SPEC_GATE == rejections.SINCE_SPEC_GATE
    body = lambda f: inspect.getsource(f).split(":", 1)[1].split("\n", 1)[1]  # noqa: E731 -- the body after the signature
    assert body(p5.classify_rejection).replace("  # the pinned test already passed on the base: it cannot judge the change", "") == \
        body(rejections.classify).replace("  # the pinned test already passed on the base: it cannot judge the change", "")


def _rejected(db, report, *, payload=None, failures=None, verdict=None, error=None):
    cand = CodeCandidate(id=uuid.uuid4(), correlation_id=uuid.uuid4(), requested_by_agent_id=report.agents["architect"], title="r", status="rejected",
                         spec={"kind": "code", "files_allowed": ["src/a.py"]}, qa_report={"failures": failures} if failures else {},
                         security_report={"verdict": verdict} if verdict else {}, error=error)
    db.add(cand)
    db.flush()
    if payload is not None:
        emit_event(db, event_type=EventType.CODE_CANDIDATE_REJECTED, payload={"candidate_id": str(cand.id), **payload}, actor_type="system",
                   subject_type="code_candidate", subject_id=cand.id, correlation_id=cand.correlation_id, idempotency_key=f"t-rej-{cand.id}")
    db.commit()
    return cand


def test_the_breakdown_reaches_the_report_the_digest_and_the_validator(db, engine, society_settings):
    report = seed_society(db)
    _rejected(db, report, payload={"declined": True, "reason_code": "spec_outside_files_allowed"})
    _rejected(db, report, failures=["no_self_judging: x"])
    _rejected(db, report, failures=["no_self_judging: y"])
    _rejected(db, report, error="closed_by_owner: pull request closed without merge (PR #115)")
    old = _rejected(db, report, payload={"busywork": True})
    db.query(CodeCandidate).filter(CodeCandidate.id == old.id).update({"updated_at": rejections.SINCE_SPEC_GATE - timedelta(days=1)})
    ev = emit_event(db, event_type=EventType.SOCIETY_HEARTBEAT, payload={}, actor_type="system", idempotency_key=f"t-ev-{uuid.uuid4()}")
    run = AgentRun(id=uuid.uuid4(), agent_id=report.agents["architect"], event_id=ev.id, role="architect", status="succeeded", correlation_id=uuid.uuid4(), context_summary={}, intents_count=1, cost_usd=0)
    db.add(run)
    db.flush()
    db.add(AgentIntent(id=uuid.uuid4(), run_id=run.id, agent_id=report.agents["architect"], seq=0, intent_type="PROPOSE_DESIGN", payload={}, idempotency_key=f"t-gate-{run.id}",
                       execution_status="failed", error="spec gate: acceptance test tests/x.py is not on main"))
    db.commit()
    b = rejections.breakdown(db)
    assert b["rejected"] == 4 and b["by_reason"] == {"no_self_judging": 2, "closed_by_owner": 1, "outside_files_allowed": 1} and b["spec_gate_refusals"] == 1
    line = rejections.summary(b)
    assert line == "Rejected since 2026-10-27: 4 (no_self_judging 2, closed_by_owner 1, outside_files_allowed 1); spec gate refused 1 design(s) before a build"
    assert line in daily_report.build(db, utcnow())["text"]
    assert line in boardroom.digest(db, society_settings)
    conn = engine.raw_connection()
    try:
        with conn.cursor() as cur:
            assert _p5().rejection_breakdown(cur) == b
        conn.rollback()
    finally:
        conn.close()


# ── design-review limits: stated in the prompt, fitted deterministically ──────────────────


def test_the_prompts_state_the_exact_limits():
    from services.registry.app.society import cognition, design_review

    assert "1-5 items, each 3-300 chars" in cognition.SYSTEM_PROMPT and "10-800 chars" in cognition.SYSTEM_PROMPT
    review = {"round": 1, "proposal": {}, "positions": {}}
    assert POINTS_LIMIT in design_review._question("reviewing", "qa", review, "t", "soon")
    assert "at most 4 items" in design_review._question("reviewing", "security", review, "t", "soon")
    assert CONDITIONS_LIMIT in design_review._question("deciding", "governor", review, "t", "soon")


def _decision(itype, payload):
    return AgentDecision.model_validate({"decision_summary": "x", "intents": [{"type": itype, "payload": payload}]})


RID = str(uuid.uuid4())


def test_points_are_fitted_never_rejected_for_length_or_count():
    long = "p" * 420
    raw = {"review_id": RID, "stance": "concern", "points": ["ok point", "  ", "x", long, "a3", "b4 b4", "c5 c5", "d6 d6", "e7 e7"]}
    (v,) = validate_intents(_decision("REVIEW_DESIGN", raw), uuid.uuid4())
    assert v.valid, v.error
    assert v.payload.points == ["ok point", "p" * 297 + "...", "b4 b4", "c5 c5", "d6 d6"]
    assert v.raw_payload == raw, "the audit keeps what the model wrote"
    fitted, notes = normalize_payload(IntentType.REVIEW_DESIGN, {"review_id": RID, "stance": "support", "points": ["fine"]})
    assert notes == [] and fitted["points"] == ["fine"]


def test_constraints_and_conditions_are_cut_but_never_dropped():
    many = [f"S constraint {i}" for i in range(6)]
    (v,) = validate_intents(_decision("REVIEW_DESIGN", {"review_id": RID, "stance": "object", "points": ["no go"], "constraints": many}), uuid.uuid4())
    assert not v.valid and "too_long" in v.error, "a Security constraint is never silently dropped"
    (v,) = validate_intents(_decision("REVIEW_DESIGN", {"review_id": RID, "stance": "object", "points": ["no go"], "constraints": ["c" * 500]}), uuid.uuid4())
    assert v.valid and v.payload.constraints == ["c" * 297 + "..."]
    (v,) = validate_intents(_decision("DECIDE_DESIGN", {"review_id": RID, "verdict": "revise", "rationale": "r" * 1200, "conditions": ["k" * 400]}), uuid.uuid4())
    assert v.valid and len(v.payload.rationale) == 800 and v.payload.conditions == ["k" * 297 + "..."]
    (v,) = validate_intents(_decision("DECIDE_DESIGN", {"review_id": RID, "verdict": "go", "rationale": "short", "conditions": []}), uuid.uuid4())
    assert not v.valid, "too SHORT is still the model's to fix"
    other, notes = normalize_payload(IntentType.WRITE_MEMORY, {"title": "t" * 900})
    assert notes == [] and other == {"title": "t" * 900}, "no other intent is touched"
