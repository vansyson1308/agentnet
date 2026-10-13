"""Staging 2026-10-12: candidate c0b459e1 (ticket 321b19bf, the only RED slot) was rebuilt 6x in a day,
changed_files [] every time, its last error naming a red acceptance test with error class turn_budget --
so DECLINE acceptance_unsatisfiable (which wanted tests_failing) stayed refused, and nothing else could
end it. Three deterministic exits, no operator and no model (executor.py, candidate_health.py):

* baseline gate: before a candidate's first committed build, the spec's guard tests (acceptance tests
  that are neither the ticket's proof nor in files_allowed) run on the untouched base; one red there
  means no patch within the spec can pass -> REJECTED, ticket back to proposed with the ids;
* build cap: SOCIETY_MAX_BUILD_ATTEMPTS failed builds without a committed head -> REJECTED by code;
* decline evidence: any failed build that NAMED failing acceptance tests (any error class).

Staging 2026-10-13: the gate rejected b3b2cd9e for tests/test_bench.py -- a regression test the
work packet itself put in EVERY bench spec. A recent red-on-base verdict now keeps that file out
of the packet and the spec, and the gate records the first failure reason (diagnosis)."""

from __future__ import annotations

import dataclasses
import subprocess
import uuid

from sqlalchemy import text

from services.registry.app.models import AgentIntent, AgentRun, CodeCandidate, SocietyEvent
from services.registry.app.society import candidate_health, tickets, work_packet
from services.registry.app.society.engineering import bench_proof
from services.registry.app.society.events import EventType, emit_event
from services.registry.app.society.seed import seed_society

from .test_build_engine import RED, SRC, SUBMIT, TESTS, _build, _candidate, _ev

ENV_TEST = "tests/test_env_guard.py::test_needs_a_service_the_sandbox_lacks"


def _red_on_base(repo):
    (repo / "tests" / "test_env_guard.py").write_text("def test_needs_a_service_the_sandbox_lacks():\n    assert False, 'no such service here'\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "an environment-red test"], cwd=repo, check=True)


def _ticketed(db, report, *, files=(SRC,), tests=(*TESTS, ENV_TEST)):
    cand = _candidate(db, report)
    cand.spec = {**cand.spec, "files_allowed": list(files), "acceptance_tests": list(tests)}
    tid = tickets.create(db, proposal_id=cand.proposal_id, title=cand.title, fields={"objective_id": "O3", "metric_id": "bench_holdout_pass_at_1",
                         "expected_effect": 0.01, "direction": "up", "proof": list(TESTS)}, source="owner", role="governor", importance=60)
    tickets.set_status(db, tid, "building", candidate_id=cand.id)
    db.commit()
    return cand, tid


def _ticket(db, tid):
    return dict(db.execute(text("SELECT * FROM society_tickets WHERE id = :i"), {"i": tid}).mappings().one())


def test_a_guard_test_red_on_the_base_rejects_the_candidate_before_any_model_call(db, SessionLocal, code_settings, code_repo, grants_with_no_cooldown):
    _red_on_base(code_repo)
    report = seed_society(db)
    grants_with_no_cooldown()
    cand, tid = _ticketed(db, report)
    row = _build(db, SessionLocal, code_settings, cand, [])  # an empty script: a model call would fail the build
    assert _ev(row.execution_status) == "executed" and row.result["result"]["red_on_base"] == [ENV_TEST], row.error
    db.refresh(cand)
    assert _ev(cand.status) == "rejected" and cand.error.startswith(f"acceptance test red on base: ['{ENV_TEST}']")
    assert "no such service here" in cand.error, "the first failure reason is kept for diagnosis"
    t = _ticket(db, tid)
    assert t["status"] == "proposed" and t["plan_id"] is None and t["reason"].startswith(f"acceptance test red on base: ['{ENV_TEST}']")
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.CODE_CANDIDATE_REJECTED, SocietyEvent.subject_id == cand.id).count() == 1


def test_the_proof_and_files_the_spec_may_change_are_allowed_red_on_the_base(db, SessionLocal, code_settings, code_repo, grants_with_no_cooldown):
    _red_on_base(code_repo)
    report = seed_society(db)
    grants_with_no_cooldown()
    cand, _ = _ticketed(db, report, files=(SRC, "tests/test_env_guard.py"))  # the proof TESTS[0] is red too
    row = _build(db, SessionLocal, code_settings, cand, [RED] + [SUBMIT] * 20)
    assert _ev(row.execution_status) == "failed" and "build failed" in row.error, "the gate let it through to the model"


def _failed_build(db, report, cand, error):
    ev = emit_event(db, event_type="t.build", payload={}, correlation_id=cand.correlation_id, idempotency_key=f"t-{uuid.uuid4()}")
    run = AgentRun(id=uuid.uuid4(), agent_id=report.agents["builder"], event_id=ev.id, role="builder", status="completed", correlation_id=cand.correlation_id,
                   context_summary={}, intents_count=1, cost_usd=0)
    db.add(run)
    db.flush()
    db.add(AgentIntent(run_id=run.id, agent_id=run.agent_id, seq=0, intent_type="BUILD_CODE_CANDIDATE", payload={"candidate_id": str(cand.id)},
                       idempotency_key=f"{run.id}:0", risk_class="medium", policy_decision="allow", execution_status="failed", error=error))
    db.commit()


def test_a_candidate_that_failed_its_build_cap_is_rejected_by_code_and_frees_its_ticket(db, code_settings, grants_with_no_cooldown):
    report = seed_society(db)
    settings = dataclasses.replace(code_settings, max_build_attempts=3)
    cand, tid = _ticketed(db, report)
    failing = "build failed after 10 turns, 2 test runs: no green worktree (turn_budget: no result within 10 turns; failing ['tests/a.py::t'])"
    for _ in range(2):
        _failed_build(db, report, cand, failing)
    _failed_build(db, report, cand, "portfolio full: 1 open high-risk investigation(s)")  # not a build: never counted
    cand.error = "build failed: no green worktree (turn_budget: no result within 10 turns; failing ['tests/a.py::t']); BUILD again or DECLINE_CODE_CANDIDATE"
    db.commit()
    assert candidate_health.sweep(db, settings)["build_cap_rejected"] == 0
    _failed_build(db, report, cand, failing)
    assert candidate_health.sweep(db, settings)["build_cap_rejected"] == 1
    db.refresh(cand)
    assert _ev(cand.status) == "rejected" and cand.error.startswith("rejected by the runtime: 3 builds failed without a green worktree (SOCIETY_MAX_BUILD_ATTEMPTS=3)")
    t = _ticket(db, tid)
    assert t["status"] == "proposed" and t["plan_id"] is None and t["candidate_id"] is None, "the ticket no longer points at a dead candidate"
    assert "3 builds of candidate" in t["reason"] and "failing ['tests/a.py::t']" in t["reason"]
    assert candidate_health.sweep(db, settings)["build_cap_rejected"] == 0, "idempotent"
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.CODE_CANDIDATE_REJECTED, SocietyEvent.subject_id == cand.id).count() == 1
    built = _candidate(db, report)  # a committed head is a surviving diff: QA judges it, the cap does not
    built.head_sha = "f" * 40
    db.commit()
    for _ in range(3):
        _failed_build(db, report, built, failing)
    assert candidate_health.sweep(db, settings)["build_cap_rejected"] == 0 and _ev(db.get(CodeCandidate, built.id).status) == "requested"


def test_the_supply_bar_is_the_bench_proof_bar_and_dead_worker_reasons_are_relabelled_once(db, company):  # noqa: F811
    assert tickets.bench_met(bench_proof.MIN_DELIVERED, bench_proof.REPEAT) and not tickets.bench_met(bench_proof.MIN_DELIVERED - 1, bench_proof.REPEAT)
    legacy = "refused 2x by REQUEST_CODE_CHANGE, back to proposed: no REQUEST_CODE_CHANGE within 12h of approval"
    tid = uuid.uuid4()
    db.execute(text("INSERT INTO society_tickets (id, title, objective_id, metric_id, expected_effect, direction, source, department, reason) "
                    "VALUES (:i, 't', 'O3', 'bench_holdout_pass_at_1', 0.01, 'up', 'backlog', 'engineering', :r)"), {"i": tid, "r": legacy})
    assert tickets.relabel_dead_worker_refusals(db) == 1 and tickets.relabel_dead_worker_refusals(db) == 0
    assert _ticket(db, tid)["reason"] == tickets.DEAD_WORKER_RELABEL


def test_a_test_file_red_on_base_leaves_the_bench_packet_and_the_spec(db, SessionLocal, company):  # noqa: F811
    report, settings = company
    _activate(db)
    _wp_report(db, {TASK: {"split": "dev", "delivered": 1, "runs": ["turn_budget", "pass", "turn_budget"], "detail": DETAIL}})
    bench = "tests/test_bench.py"
    assert bench in work_packet.bench_packet(db, TASK)["regression_tests"]
    emit_event(db, event_type=EventType.CODE_CANDIDATE_REJECTED, payload={"red_on_base": [f"{bench}::test_an_external_task"]}, idempotency_key=f"t-{uuid.uuid4()}")
    db.commit()
    pk = work_packet.bench_packet(db, TASK)
    assert pk["red_on_base"] == [bench] and pk["regression_tests"] == [t for t in work_packet.REGRESSION_TESTS if t != bench]
    p, _ = _bench_ticket(db, report)
    keep = "tests/society/maintenance/test_activity_loop.py"
    row = _run(db, SessionLocal, settings, "architect", [_bench_request(p, [work_packet.HARNESS], tests=[keep, bench])])
    assert row.execution_status.value == "executed", row.error
    cand = db.query(CodeCandidate).one()
    assert cand.spec["acceptance_tests"] == [keep, f"bench:{TASK}"] and cand.spec["dropped_red_on_base"] == [bench]
    db.execute(text("UPDATE code_candidates SET status = 'rejected'"))
    db.commit()
    row = _run(db, SessionLocal, settings, "architect", [_bench_request(p, [work_packet.HARNESS, bench], tests=[bench])])
    assert row.execution_status.value == "executed", row.error
    fixable = db.query(CodeCandidate).filter(CodeCandidate.id != cand.id).one()
    assert fixable.spec["acceptance_tests"] == [bench, f"bench:{TASK}"], "a red test the spec lets the Builder fix stays"
    db.execute(text("UPDATE society_events SET created_at = now() - interval '8 days' WHERE payload ? 'red_on_base'"))
    assert work_packet.red_on_base_files(db) == [], "a week later the file is named again (the gate re-checks it)"


from .test_company_tickets import _activate, _run, company  # noqa: E402,F401 -- fixture
from .test_ticket_work_packet import DETAIL, TASK, _bench_request, _bench_ticket  # noqa: E402
from .test_ticket_work_packet import _report as _wp_report  # noqa: E402
