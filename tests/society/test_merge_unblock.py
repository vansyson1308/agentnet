"""G1y (owner 2026-10-31): three things kept every AMBER bot PR from merging.

a) the open-PR cap (``SOCIETY_MAX_OPEN_AUTONOMOUS_PRS``) was a MERGE gate, so
   once more PRs were open than the cap nothing could merge -- and merging is
   the only thing that lowers the count. The cap gates OPENING a PR only.
b) a revived promotion (it resumes an existing PR) counted as a new promotion
   and ate the daily cap.
c) fitness: experiments of promotions the controller may merge itself are
   claimed first; the story's model cost, QA attempts and denied intents are
   process observations, not a regression of the change; a test-duration
   delta between DIFFERENT test sets is not a performance verdict; a target
   absent on base (pytest exit 4 "not found") is treated like exit 5; an
   experiment judged by older criteria that did not satisfy the gate is
   re-requested once under the current criteria; and the validator shows the
   fitness fields of every promotion.
"""

from __future__ import annotations

import importlib.util
import pathlib
import uuid
from datetime import timedelta

import pytest

from services.registry.app.models import ChangeExperiment, CodePromotion, PromotionStatus
from services.registry.app.society import fitness
from services.registry.app.society import promotion as pm
from services.registry.app.society import promotion_queue
from services.registry.app.society.config import reset_settings_cache
from services.registry.app.society.events import utcnow
from services.registry.app.society.seed import seed_society

from .test_fitness import _src, make_candidate, run_experiment
from .test_promotion import _automerge_settings, _ev, _experiment_pass, _request, drive, ready_candidate

AMBER_PATH = "sdk/python/agentnet/hint.py"
FIXED_LINE = '_TRUE = {"true", "1", "yes", "on", "y"}'


@pytest.fixture(autouse=True)
def _reset_settings():
    yield
    reset_settings_cache()


def _parked_open_pr(db, settings, report, n):
    """An open PR of another candidate that this worker must not touch (leased elsewhere)."""
    cand = ready_candidate(db, settings, report, path=f"docs/society/candidates/parked{n}.md", content=f"# parked {n}\n", title=f"parked{n}")
    promo = CodePromotion(id=uuid.uuid4(), candidate_id=cand.id, correlation_id=cand.correlation_id, risk_tier="green", provider="fake",
                          status=PromotionStatus.PR_OPEN, base_sha=cand.base_sha, candidate_sha=cand.head_sha, external_pr_number=900 + n,
                          worker_id="elsewhere", lease_expires_at=utcnow() + timedelta(days=1), evidence={}, eligibility={})
    db.add(promo)
    db.commit()
    return promo


# ── a) the open-PR cap gates opening, never merging ─────────────────────


def test_the_open_pr_cap_never_blocks_merging_an_already_open_pr(db, db_factory, temp_repo, tmp_path, monkeypatch):
    settings = _automerge_settings(monkeypatch, temp_repo, tmp_path, SOCIETY_AUTO_MERGE_MAX_TIER="amber", SOCIETY_MAX_OPEN_AUTONOMOUS_PRS="1")
    provider = pm.FakePromotionProvider()
    report = seed_society(db)
    cand = ready_candidate(db, settings, report, path=AMBER_PATH, content="# amber\n", kind="code", title="amber")
    promo, created = _request(db, settings, cand, report)
    assert created
    for n in range(3):  # staging 2026-10-31: 14 open PRs > cap 8
        _parked_open_pr(db, settings, report, n)
    assert pm.open_prs(db) > settings.max_open_autonomous_prs
    drive(db_factory, settings, provider)
    _experiment_pass(db, promo, cand)
    drive(db_factory, settings, provider)
    db.refresh(promo)
    assert "change_budget" not in promo.eligibility["blocking"]
    assert _ev(promo.status) == "merged" and provider.merge_count == 1
    # ... while OPENING another PR is still refused by the same cap
    other = ready_candidate(db, settings, report, path="docs/society/candidates/other.md", title="other")
    with pytest.raises(ValueError, match="autonomous PRs already open"):
        _request(db, settings, other, report)


# ── b) a revived promotion is not a new promotion ───────────────────────


def test_a_revived_promotion_does_not_spend_the_daily_promotion_budget(db, temp_repo, tmp_path, monkeypatch):
    settings = _automerge_settings(monkeypatch, temp_repo, tmp_path, SOCIETY_MAX_PROMOTIONS_PER_DAY="1")
    report = seed_society(db)
    old = ready_candidate(db, settings, report, path="docs/society/candidates/old.md", title="old")
    revived = CodePromotion(id=uuid.uuid4(), candidate_id=old.id, correlation_id=old.correlation_id, risk_tier="green", provider="fake",
                            status=PromotionStatus.PR_OPEN, base_sha=old.base_sha, candidate_sha=old.head_sha, external_pr_number=901,
                            evidence={"revived_from": str(uuid.uuid4()), "revivals": 1}, eligibility={})
    db.add(revived)
    db.commit()
    assert pm.promotions_today(db) == 0
    assert promotion_queue.caps(db, settings)["promotions_today"] == 0
    fresh = ready_candidate(db, settings, report, path="docs/society/candidates/fresh.md", title="fresh")
    promo, created = _request(db, settings, fresh, report)
    assert created and pm.promotions_today(db) == 1
    third = ready_candidate(db, settings, report, path="docs/society/candidates/third.md", title="third")
    with pytest.raises(ValueError, match="promotions today"):
        _request(db, settings, third, report)


# ── c) fitness ──────────────────────────────────────────────────────────


def _planned(db, cand, promo, created_at):
    exp = ChangeExperiment(id=uuid.uuid4(), candidate_id=cand.id, promotion_id=promo.id, correlation_id=cand.correlation_id,
                           baseline_sha=cand.base_sha, candidate_sha=cand.head_sha, status="planned",
                           criteria_snapshot={"version": fitness.CRITERIA_VERSION}, created_at=created_at)
    db.add(exp)
    db.commit()
    return exp


def test_experiments_of_self_mergeable_promotions_are_claimed_first(db, temp_repo, tmp_path, monkeypatch):
    settings = _automerge_settings(monkeypatch, temp_repo, tmp_path)
    report = seed_society(db)
    now = utcnow()
    rows = {}
    for name, reasons, age in (("red", ["risk_tier_red_above_amber"], 3), ("unknown", None, 2), ("amber", [], 1)):
        cand = ready_candidate(db, settings, report, path=f"docs/society/candidates/{name}.md", title=name)
        elig = {} if reasons is None else {"owner_merge_reasons": reasons}
        promo = CodePromotion(id=uuid.uuid4(), candidate_id=cand.id, correlation_id=cand.correlation_id, risk_tier="amber", provider="fake",
                              status=PromotionStatus.CI_PASSED, base_sha=cand.base_sha, candidate_sha=cand.head_sha, eligibility=elig, evidence={})
        db.add(promo)
        db.commit()
        rows[name] = _planned(db, cand, promo, now - timedelta(hours=age))
    order = []
    for _ in range(3):
        exp = fitness.claim_next_experiment(db, worker_id="w", lease_seconds=600)
        order.append(exp.id)
    # the RED one waits for the owner anyway: it is evaluated last, oldest-first otherwise
    assert order == [rows["unknown"].id, rows["amber"].id, rows["red"].id]


@pytest.mark.timeout(300)
def test_story_cost_qa_attempts_and_denied_intents_are_observed_not_regressions(db, code_settings, code_repo, monkeypatch):
    """Staging 2026-10-31: with Claude every ticket story costs more than $0.20, so the
    fitness-v1 rule ``economics.model_cost_usd regress_abs_over 0.20`` failed EVERY candidate
    ("regression") whatever its code did. The story's spend is not a property of the change."""
    monkeypatch.setattr(fitness, "_correlation_costs", lambda db, cid: {"model_cost_usd": 4.75, "intents_denied": 9, "runs_dead": 0, "loop_breaks": 0})
    fixed = _src(code_repo).replace('_TRUE = {"true", "1"}', FIXED_LINE)
    cand, _ = make_candidate(db, code_settings, code_repo, [("app/textutil.py", fixed)])
    cand.qa_report = {"verdict": "pass", "attempts": 4}
    db.commit()
    exp = run_experiment(db, code_settings, cand)
    for key in ("economics.model_cost_usd", "autonomy.qa_attempts", "autonomy.intents_denied"):
        assert exp.metric_deltas[key]["verdict"] == "observed", key
    assert exp.candidate_metrics["economics"]["model_cost_usd"] == 4.75  # still recorded
    assert exp.decision == "pass" and exp.confidence == "high"
    assert (exp.criteria_snapshot or {}).get("version") == fitness.CRITERIA_VERSION == "fitness-v2"


@pytest.mark.timeout(300)
def test_a_new_test_file_absent_on_base_is_not_a_duration_regression(db, code_settings, code_repo):
    """A docs/template candidate adds its own test: base runs fewer tests than the candidate, so
    their durations are not comparable (fitness-v1 called +1s over a 0s base a regression)."""
    new_test = "import time\n\n\ndef test_new_section_exists():\n    time.sleep(1.2)\n    assert True\n"
    cand, _ = make_candidate(db, code_settings, code_repo, [("tests/test_new_section.py", new_test)], targets=["tests/test_new_section.py", "tests/test_textutil.py"], kind="docs")
    exp = run_experiment(db, code_settings, cand)
    assert exp.metric_deltas["performance.test_duration_s"]["verdict"] == "observed"
    assert exp.decision == "pass" and exp.confidence == "high"
    assert pm._fitness_satisfied(exp)


@pytest.mark.timeout(300)
def test_a_test_function_absent_on_base_is_treated_like_exit_5(db, code_settings, code_repo):
    """pytest exits 4 ("ERROR: not found: <file>::<test>") when the base has the file but not
    the candidate's new test function. That target simply does not exist at the base revision."""
    body = (code_repo / "tests" / "test_textutil.py").read_text()
    body += "\n\ndef test_brand_new_check():\n    assert 1 + 1 == 2\n"
    cand, _ = make_candidate(db, code_settings, code_repo, [("tests/test_textutil.py", body)],
                             targets=["tests/test_textutil.py::test_brand_new_check", "tests/acceptance/test_parse_bool_regression.py"], kind="docs")
    exp = run_experiment(db, code_settings, cand)
    base = exp.baseline_metrics["_tests"]
    assert base["exit_code"] != 4 and base["absent_targets"] == ["tests/test_textutil.py::test_brand_new_check"]
    assert base["collected"] > 0, "the targets that DO exist on base still ran"
    assert exp.confidence == "high"
    only_new = fitness._run_pytest(pathlib.Path(code_settings.repo_root), ["tests/test_textutil.py::test_brand_new_check"], timeout=120)
    assert only_new.exit_code == 5 and only_new.absent_targets == ["tests/test_textutil.py::test_brand_new_check"]


def test_an_unsatisfied_experiment_under_old_criteria_is_requested_again_once(db, temp_repo, tmp_path, monkeypatch):
    settings = _automerge_settings(monkeypatch, temp_repo, tmp_path)
    report = seed_society(db)
    cand = ready_candidate(db, settings, report, path="docs/society/candidates/again.md", title="again")
    promo = CodePromotion(id=uuid.uuid4(), candidate_id=cand.id, correlation_id=cand.correlation_id, risk_tier="amber", provider="fake",
                          status=PromotionStatus.CI_PASSED, base_sha=cand.base_sha, candidate_sha=cand.head_sha, eligibility={}, evidence={})
    db.add(promo)
    db.commit()
    old = ChangeExperiment(id=uuid.uuid4(), candidate_id=cand.id, promotion_id=promo.id, correlation_id=cand.correlation_id,
                           baseline_sha=cand.base_sha, candidate_sha=cand.head_sha, status="fail", decision="fail", confidence="high",
                           criteria_snapshot={"version": "fitness-v1"}, created_at=utcnow() - timedelta(hours=2))
    db.add(old)
    db.commit()
    assert pm.ensure_fitness_requested(db, settings, promo, cand) is True
    db.commit()
    new = pm.latest_experiment(db, promo)
    assert new.id != old.id and _ev(new.status) == "planned" and new.criteria_snapshot["version"] == fitness.CRITERIA_VERSION
    # the current criteria judge it now: a fail under them is final (no loop)
    new.status, new.decision = "fail", "fail"
    db.commit()
    assert pm.ensure_fitness_requested(db, settings, promo, cand) is False
    # an old experiment that DID satisfy the gate is never redone
    cand2 = ready_candidate(db, settings, report, path="docs/society/candidates/kept.md", title="kept")
    promo2 = CodePromotion(id=uuid.uuid4(), candidate_id=cand2.id, correlation_id=cand2.correlation_id, risk_tier="amber", provider="fake",
                           status=PromotionStatus.CI_PASSED, base_sha=cand2.base_sha, candidate_sha=cand2.head_sha, eligibility={}, evidence={})
    db.add(promo2)
    db.add(ChangeExperiment(id=uuid.uuid4(), candidate_id=cand2.id, promotion_id=promo2.id, correlation_id=cand2.correlation_id,
                            baseline_sha=cand2.base_sha, candidate_sha=cand2.head_sha, status="pass", decision="pass", confidence="high",
                            criteria_snapshot={"version": "fitness-v1"}))
    db.commit()
    assert pm.ensure_fitness_requested(db, settings, promo2, cand2) is False


def test_the_validator_promotions_view_shows_the_fitness_of_each_record(db, engine, temp_repo, tmp_path, monkeypatch):
    settings = _automerge_settings(monkeypatch, temp_repo, tmp_path)
    report = seed_society(db)
    cand = ready_candidate(db, settings, report, path="docs/society/candidates/view.md", title="view")
    promo = CodePromotion(id=uuid.uuid4(), candidate_id=cand.id, correlation_id=cand.correlation_id, risk_tier="amber", provider="fake",
                          status=PromotionStatus.CI_PASSED, base_sha=cand.base_sha, candidate_sha=cand.head_sha, evidence={},
                          eligibility={"blocking": ["fitness_precheck"], "fitness_precheck": False, "owner_merge_reasons": []})
    db.add(promo)
    db.add(ChangeExperiment(id=uuid.uuid4(), candidate_id=cand.id, promotion_id=promo.id, correlation_id=cand.correlation_id,
                            baseline_sha=cand.base_sha, candidate_sha=cand.head_sha, status="fail", decision="fail", confidence="high", attempt=1,
                            criteria_snapshot={"version": "fitness-v1"}, error=None,
                            hard_gate_results=[{"gate": "no_test_regression", "passed": True}, {"gate": "candidate_tests_completed", "passed": False}],
                            metric_deltas={"economics.model_cost_usd": {"verdict": "regression"}, "correctness.tests_passed": {"verdict": "improvement"}}))
    db.commit()
    spec = importlib.util.spec_from_file_location("phase5_live_fit", pathlib.Path(__file__).resolve().parents[2] / "deploy/railway/phase5_live.py")
    p5 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p5)
    conn = engine.raw_connection()
    try:
        view = p5.promotion_records(conn, 20)
        conn.rollback()
    finally:
        conn.close()
    (rec,) = [v for v in view if v["promotion"] == str(promo.id)[:8]]
    assert rec["fitness"] == {"status": "fail", "decision": "fail", "confidence": "high", "criteria": "fitness-v1", "attempt": 1, "error": None,
                              "failed_hard_gates": ["candidate_tests_completed"], "regressions": ["economics.model_cost_usd"]}
    assert rec["gates"]["blocking"] == ["fitness_precheck"] and rec["gates"]["owner_merge_reasons"] == []


def test_reviewer_notes_on_a_security_pass_do_not_freeze_auto_merge(db, db_factory, temp_repo, tmp_path, monkeypatch):
    """Every bot PR on staging carries 3-5 reviewer notes on a Security PASS; under the old
    rule ``security_findings_present`` froze the auto-merge of each one. Static findings still freeze."""
    settings = _automerge_settings(monkeypatch, temp_repo, tmp_path, SOCIETY_AUTO_MERGE_MAX_TIER="amber")
    provider = pm.FakePromotionProvider()
    report = seed_society(db)
    cand = ready_candidate(db, settings, report, path=AMBER_PATH, content="# notes\n", kind="code", title="notes")
    cand.security_report = {**(cand.security_report or {}), "verdict": "pass", "findings": ["escape the link text", "keep it static"], "static_findings": []}
    db.commit()
    promo, _ = _request(db, settings, cand, report)
    drive(db_factory, settings, provider)
    assert "security_findings_present" not in pm.merge_freeze_reasons(db, settings, promo, cand)
    cand.security_report = {**cand.security_report, "static_findings": ["eval( in template"]}
    assert "security_findings_present" in pm.merge_freeze_reasons(db, settings, promo, cand)
    cand.security_report = {"verdict": "fail", "findings": ["unsafe"], "static_findings": []}
    assert "security_findings_present" in pm.merge_freeze_reasons(db, settings, promo, cand)
    cand.security_report = {"verdict": "pass", "findings": ["escape the link text"], "static_findings": []}
    db.commit()
    _experiment_pass(db, promo, cand)
    drive(db_factory, settings, provider)
    drive(db_factory, settings, provider)
    db.refresh(promo)
    assert _ev(promo.status) == "merged" and provider.merge_count == 1, {k: promo.eligibility.get(k) for k in ("auto_merge_allowed", "owner_merge_reasons", "pr_not_draft", "merge_budget_remaining")}
