"""The design-time spec gate (spec_gate.py): a design that cannot become READY is refused in the
review, and a new acceptance test is the Architect's (test_first), never the Builder's.

Staging 2026-10-26, 4/4 candidates REJECTED for design faults:
- ffe44239: the only acceptance test was a file the Builder had to create -> QA no_self_judging;
- 46b27127: a dashboard template whose view (main.py) was outside files_allowed;
- 7ae459d7: wallet.html -- a risky surface the Security static scan fails for any change (and a
  template no view renders)."""

from __future__ import annotations

import asyncio
import dataclasses
import pathlib
import uuid

from services.registry.app.maintenance.activities import ScriptedActivityModel
from services.registry.app.models import Agent, CodeCandidate, SocietyEvent
from services.registry.app.society import design_review, spec_gate, tickets, work_packet
from services.registry.app.society.cognition import FakeModel
from services.registry.app.society.events import EventType
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker

from .test_build_engine import FIX, SRC, SUBMIT, TESTS, _build, _candidate, _ev
from .test_company_tickets import company  # noqa: F401 -- fixture
from .test_design_review import IDLE, _act, _approved_ticket, _review_id, review_on  # noqa: F401 -- fixture

REPO = pathlib.Path(__file__).resolve().parents[2]
TPL = "services/dashboard/app/templates/"
NEW_TEST = "tests/test_parse_bool_on.py"
FAILS_ON_BASE = "from app.textutil import parse_bool\n\n\ndef test_on_is_true():\n    assert parse_bool('on') is True\n"


def test_the_staging_faults_are_named_before_any_build():
    # ffe44239: a new test the Builder would write
    assert any("may not write its own judge" in p for p in spec_gate.check_files(REPO, [f"{TPL}landing.html", "tests/test_landing_cta.py"],
                                                                                  ["tests/test_landing_cta.py"], unchanged=["services/dashboard/app/main.py: static page"]))
    assert any("does not exist on main" in p for p in spec_gate.check_files(REPO, [f"{TPL}landing.html"], ["tests/test_landing_cta.py"]))
    # 46b27127: a rendered template without its view
    (p,) = spec_gate.check_files(REPO, [f"{TPL}marketplace.html"], ["services/dashboard/tests/test_main.py"])
    assert "rendered by services/dashboard/app/main.py" in p
    assert spec_gate.check_files(REPO, [f"{TPL}marketplace.html", "services/dashboard/app/main.py"], ["services/dashboard/tests/test_main.py"]) == []
    assert spec_gate.check_files(REPO, [f"{TPL}marketplace.html"], ["services/dashboard/tests/test_main.py"],
                                 unchanged=["services/dashboard/app/main.py: the view already passes prices"]) == []
    # 7ae459d7: wallet.html is rendered by no view, and a risky surface
    problems = spec_gate.check_files(REPO, [f"{TPL}wallet.html"], ["services/dashboard/tests/test_main.py"])
    assert any("not rendered by any view" in p for p in problems) and any("risky surface" in p for p in problems)
    assert "base.html" in spec_gate.template_views(REPO), "a template a rendered one extends is live too"


def test_a_test_first_file_is_new_a_test_path_and_never_the_builders():
    ok = spec_gate.check_files(REPO, ["examples/agent_sdk.py"], ["tests/test_hire_and_pay_example.py"], ["tests/test_hire_and_pay_example.py"])
    assert ok == []
    bad = spec_gate.check_files(REPO, ["examples/agent_sdk.py", "tests/test_x_new.py"], ["tests/test_x_new.py", "examples/x.py"],
                                ["tests/test_x_new.py", "examples/x.py", "tests/test_db_parity.py"])
    assert any("test_first tests/test_x_new.py is in files" in p for p in bad)
    assert any("test_first examples/x.py: must be a new" in p for p in bad)
    assert any("tests/test_db_parity.py already exists on main" in p for p in bad)
    assert any("at most 2 test_first" in p for p in bad)
    assert any("no acceptance test" in p for p in spec_gate.check_files(REPO, ["examples/agent_sdk.py"], []))


def test_every_growth_lever_is_a_rendered_template_or_not_a_template_and_never_risky():
    views = spec_gate.template_views(REPO)
    levers = {f for v in work_packet.METRIC_LEVERS.values() for f in v["levers"]}
    levers |= {f for plays in work_packet.REVENUE_PLAYS.values() for p in plays for f in p["levers"]}
    for f in sorted(levers):
        assert (REPO / f).exists(), f
        assert not f.startswith(TPL) or f[len(TPL):] in views, f"{f} is rendered by no view"
        assert not spec_gate._risky(f), f"{f} is a risky surface"


def _proposal(review_id, **over):
    return {"review_id": review_id, "approach": "extend parse_bool's affirmative set with on and prove it", "files": [SRC], "proof": "a new test fails on base",
            "acceptance_tests": [NEW_TEST], "test_first": [], **over}


def test_a_refused_design_goes_back_to_the_architect_with_the_reasons_then_returns_the_ticket(db, SessionLocal, review_on):
    report, settings = review_on
    t = _approved_ticket(db)
    seen = []

    def decide(ctx):
        if ctx.event["type"] == EventType.DESIGN_REVIEW_REQUESTED and ctx.role == "architect":
            seen.append((ctx.event["payload"].get("data") or {}).get("gate"))
            if len(seen) == 1:  # the ffe44239 design: the Builder would write its own judge
                return _act({"type": "PROPOSE_DESIGN", "payload": _proposal(_review_id(ctx), files=[SRC, NEW_TEST])})
            return _act({"type": "PROPOSE_DESIGN", "payload": _proposal(_review_id(ctx), test_first=[{"path": NEW_TEST, "content": FAILS_ON_BASE}])})
        return IDLE

    worker = SocietyWorker(SessionLocal, settings=settings, model=FakeModel(decide), worker_id="w-gate", telemetry_enabled=False, builder_model=ScriptedActivityModel([]))
    worker.routing = {}
    asyncio.run(worker.run_until_idle(max_cycles=6))
    db.expire_all()
    assert seen[0] is None and any("may not write its own judge" in p for p in seen[1]), seen
    review = design_review._row(db, "ticket_id = :t", {"t": t["id"]})
    assert review["status"] == "reviewing" and review["proposal"]["test_first"][0]["path"] == NEW_TEST
    # three refusals in one round return the ticket: no build is paid for a design that cannot pass
    t2 = _approved_ticket(db)
    r2 = design_review._row(db, "ticket_id = :t", {"t": t2["id"]})
    arch = db.get(Agent, report.agents["architect"])
    for _ in range(design_review.GATE_RETRIES + 1):
        out = design_review.gate_refuse(db, review_id=r2["id"], correlation_id=r2["correlation_id"], agent=arch, problems=["x does not exist on main"], causation=None)
    assert out["returned"] and tickets.for_proposal(db, t2["proposal_id"])["status"] == "proposed"


def _test_first_candidate(db, report, content):
    cand = _candidate(db, report)
    pinned = spec_gate.pin({"test_first": [{"path": NEW_TEST, "content": content}]})
    cand.spec = {**cand.spec, "acceptance_tests": [*TESTS, NEW_TEST], "test_first": pinned}
    db.commit()
    return cand


def test_the_architects_test_first_is_committed_before_the_build_and_qa_accepts_it_untouched(db, SessionLocal, code_settings, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    cand = _test_first_candidate(db, report, FAILS_ON_BASE)
    row = _build(db, SessionLocal, code_settings, cand, [FIX, {"action": "run_tests", "args": {}}, SUBMIT], then_scripted_roles=True)
    assert _ev(row.execution_status) == "executed", row.error
    db.refresh(cand)
    assert set(cand.changed_files) == {SRC, NEW_TEST}, "the PR carries the Architect's test and the Builder's change"
    checks = {c["name"]: c["passed"] for c in cand.qa_report["checks"]}
    assert checks["test_first_intact"] and checks["no_self_judging"] and checks["allow_list"], cand.qa_report
    assert _ev(cand.status) == "ready", (cand.status, cand.qa_report)


def test_a_test_first_that_already_passes_cannot_judge_the_change(db, SessionLocal, code_settings, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    cand = _test_first_candidate(db, report, "from app.textutil import parse_bool\n\n\ndef test_true_is_true():\n    assert parse_bool('true') is True\n")
    _build(db, SessionLocal, code_settings, cand, [])  # an empty script: a model call would fail the build
    db.refresh(cand)
    assert _ev(cand.status) == "rejected" and "already passes before the change" in cand.error


def test_a_test_first_file_counts_as_the_architects_only_while_byte_identical(tmp_path):
    pinned = spec_gate.pin({"test_first": [{"path": NEW_TEST, "content": FAILS_ON_BASE}]})
    (tmp_path / "tests").mkdir()
    (tmp_path / NEW_TEST).write_text(FAILS_ON_BASE)
    assert spec_gate.intact(tmp_path, pinned) == {NEW_TEST: True}
    (tmp_path / NEW_TEST).write_text(FAILS_ON_BASE.replace("is True", "is not None"))  # a weakened judge
    assert spec_gate.intact(tmp_path, pinned) == {NEW_TEST: False}


def test_the_executor_pins_the_approved_designs_test_first_from_the_minutes_not_the_model(db, review_on):
    report, settings = review_on
    t = _approved_ticket(db)
    review = design_review._row(db, "ticket_id = :t", {"t": t["id"]})
    arch = db.get(Agent, report.agents["architect"])
    proposal = {**_proposal(str(review["id"])), "test_first": [{"path": NEW_TEST, "content": FAILS_ON_BASE}], "unchanged": [], "risks": []}
    proposal.pop("review_id")
    design_review.propose(db, review_id=review["id"], correlation_id=review["correlation_id"], agent=arch, proposal=proposal, causation=None)
    design_review._settle(db, design_review.get(db, review["id"]), {"verdict": "go", "rationale": "ok", "conditions": [], "by": "Society_Governor"})
    db.commit()
    from services.registry.app.society.executor import _approved_design

    ctx = type("Ctx", (), {"db": db, "settings": dataclasses.replace(settings, company_cycle_enabled=True)})()
    design = _approved_design(ctx, t["proposal_id"])
    assert spec_gate.pin(design)[0]["sha256"] == spec_gate.pin({"test_first": [{"path": NEW_TEST, "content": FAILS_ON_BASE}]})[0]["sha256"]
    assert db.query(CodeCandidate).count() == 0 and db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.COMPANY_TICKET_APPROVED).count() == 1
    assert uuid.UUID(str(review["id"]))
