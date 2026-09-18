"""Deterministic end-to-end proof of the SELF-DEVELOPING loop on REAL code.

ONE synthetic anomaly is injected. Everything after it is produced by the
runtime: Scout evidence -> proposal -> Governor review -> Architect
reconnaissance (SEARCH_REPO) -> CodeChangeSpec derived from the search ->
Builder investigation (READ_REPO_FILE in the worktree) -> source-code fix +
new test -> QA (acceptance + regression on real pytest) -> independent
Security -> READY -> Governor requests promotion -> promotion controller
(shadow: FakePromotionProvider) validates from the TRUSTED base, publishes
the branch, opens a PR, observes CI -> Evaluator requests the offline
fitness experiment -> base-vs-candidate evaluation -> decision -> Evaluator
recommendation + memory -> AWAITING_APPROVAL (auto-merge OFF).

The model is the offline ScriptedRoleModel: this proves MECHANICS, not model
quality. Nothing here touches GitHub or the developer's checkout.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid

import pytest

from services.registry.app.models import (
    AgentIntent,
    AgentRun,
    ChangeExperiment,
    CodeCandidate,
    CodePromotion,
    ImprovementProposal,
    MemoryItem,
    SocietyEvent,
    Wallet,
    WalletOwnerType,
)
from services.registry.app.society.cognition import ScriptedRoleModel
from services.registry.app.society.events import EventType, emit_event
from services.registry.app.society.promotion import FakePromotionProvider
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker


def _ev(v):
    return v.value if hasattr(v, "value") else v


ANOMALY = {
    "metric": "task_failure_rate",
    "value": 0.38,
    "threshold": 0.10,
    "baseline": 0.03,
    "window_seconds": 3600,
    "sample_size": 42,
    "description": "38% of parse-input tasks failed in the last hour: affirmative answers ('yes', 'on') are rejected as false",
    "suspected_cause": "boolean input parsing treats 'yes'/'on' as false",
    "symbol": "parse_bool",
    "severity_score": 75,
}


def run_code_story(db, SessionLocal, settings, *, provider=None, max_cycles=80):
    report = seed_society(db)
    for g in db.query(__import__("services.registry.app.models", fromlist=["AgentCapabilityGrant"]).AgentCapabilityGrant).all():
        g.wake_cooldown_seconds = 0
    architect_wallet = db.query(Wallet).filter(Wallet.owner_type == WalletOwnerType.AGENT, Wallet.owner_id == report.agents["architect"]).first()
    architect_wallet.balance_credits = 100
    db.commit()
    correlation = uuid.uuid4()
    ev = emit_event(db, event_type=EventType.PLATFORM_METRIC_ANOMALY, payload=dict(ANOMALY), actor_type="system", correlation_id=correlation, idempotency_key=f"e2e-code-{correlation}")
    db.commit()
    provider = provider or FakePromotionProvider()
    worker = SocietyWorker(SessionLocal, settings=settings, model=ScriptedRoleModel(), worker_id="e2e-code", promotion_provider=provider, telemetry_enabled=False)
    stats = asyncio.run(worker.run_until_idle(max_cycles=max_cycles))
    return report, correlation, ev, provider, stats


@pytest.mark.timeout(600)
def test_one_anomaly_becomes_a_real_code_fix_shadow_pr_and_fitness_verdict(db, SessionLocal, code_settings, code_repo):
    report, correlation, ev, provider, stats = run_code_story(db, SessionLocal, code_settings)

    # ── runs: every role in the loop participated ──
    runs = db.query(AgentRun).filter(AgentRun.correlation_id == correlation).order_by(AgentRun.created_at).all()
    by_role = {}
    for r in runs:
        by_role.setdefault(r.role, []).append(r)
    for role in ("scout", "governor", "architect", "builder", "qa", "security", "evaluator"):
        assert role in by_role and any(_ev(r.status) == "completed" for r in by_role[role]), (role, [(r.role, _ev(r.status), r.error) for r in runs])
    assert stats.runs_dead == 0 and stats.loop_breaks == 0, stats.as_dict()

    # ── Scout evidence, Governor approval ──
    prop = db.query(ImprovementProposal).one()
    scout_intent = db.query(AgentIntent).filter(AgentIntent.intent_type == "CREATE_IMPROVEMENT").one()
    assert scout_intent.payload["evidence"]["signal"] == "task_failure_rate" and scout_intent.payload["evidence"]["sample_size"] == 42
    assert _ev(prop.status) == "CONVERTED_TO_TASK"

    # ── Architect reconnaissance: located the file WITHOUT being given its path ──
    search = db.query(AgentIntent).filter(AgentIntent.intent_type == "SEARCH_REPO", AgentIntent.agent_id == report.agents["architect"]).one()
    assert _ev(search.execution_status) == "executed" and search.payload["pattern"] == "parse_bool"
    assert "app/textutil.py" not in str(ANOMALY)
    cand = db.query(CodeCandidate).filter(CodeCandidate.correlation_id == correlation).one()
    assert cand.spec["kind"] == "code"
    assert cand.spec["files_allowed"][0] == "app/textutil.py"
    assert set(cand.spec["acceptance_tests"]) == {"tests/acceptance/test_parse_bool_regression.py", "tests/test_textutil.py"}
    assert cand.spec["expected_effect"]

    # ── Builder investigated the worktree before editing ──
    read = db.query(AgentIntent).filter(AgentIntent.intent_type == "READ_REPO_FILE", AgentIntent.agent_id == report.agents["builder"]).one()
    assert read.payload["candidate_id"] == str(cand.id) and _ev(read.execution_status) == "executed"
    assert cand.repo_reads >= 1 and cand.engineering_turns >= 1

    # ── REAL source-code change + test, QA on real pytest, independent Security, READY ──
    assert _ev(cand.status) == "ready", (cand.qa_report, cand.security_report, cand.error)
    assert sorted(cand.changed_files) == ["app/textutil.py", "tests/test_textutil_parse-bool_fix.py"]
    assert cand.qa_report["verdict"] == "pass" and cand.security_report["verdict"] == "pass"
    assert cand.builder_agent_id != cand.qa_agent_id != cand.security_agent_id
    assert cand.diff_lines > 0 and cand.diff_hash
    branches = subprocess.run(["git", "branch", "--list", "agentnet-auto/*"], cwd=code_repo, capture_output=True, text=True).stdout
    assert cand.branch_name in branches
    main_src = subprocess.run(["git", "show", "main:app/textutil.py"], cwd=code_repo, capture_output=True, text=True).stdout
    assert '"yes"' not in main_src, "main never moved"
    ws_src = subprocess.run(["git", "show", f"{cand.branch_name}:app/textutil.py"], cwd=code_repo, capture_output=True, text=True).stdout
    assert '"yes"' in ws_src and "on" in ws_src

    # ── promotion (shadow): requested by Governor, driven by the controller ──
    promo = db.query(CodePromotion).filter(CodePromotion.candidate_id == cand.id).one()
    assert promo.requested_by_agent_id == report.agents["governor"]
    assert promo.risk_tier == "amber" and cand.risk_tier == "amber"
    assert promo.external_branch == cand.branch_name and promo.external_pr_number
    assert provider.publish_count == 1 and provider.pr_create_count == 1 and provider.merge_count == 0
    assert promo.candidate_sha == cand.head_sha
    assert _ev(promo.status) == "awaiting_approval", (promo.status, promo.failure_reason, promo.eligibility)
    assert promo.ci_state == "passed"
    assert promo.eligibility["human_approval_required"] is True and "human_approval_satisfied" in promo.eligibility["blocking"]
    body = provider.prs[cand.branch_name]["body"]
    assert "app/textutil.py" in body and "QA" in body and "trusted risk tier" in body

    # ── fitness: base vs candidate, hard gates, PASS, memory ──
    exp = db.query(ChangeExperiment).filter(ChangeExperiment.promotion_id == promo.id).one()
    assert exp.requested_by_agent_id == report.agents["evaluator"]
    assert _ev(exp.status) == "pass" and exp.decision == "pass", (exp.hard_gate_results, exp.metric_deltas, exp.error)
    assert all(g["passed"] for g in exp.hard_gate_results)
    assert exp.criteria_snapshot["version"] == "fitness-v1"
    assert exp.baseline_metrics["correctness"]["tests_failed"] >= 1, "the regression test fails on the base revision"
    assert exp.candidate_metrics["correctness"]["tests_failed"] == 0
    assert exp.metric_deltas["correctness.tests_passed"]["verdict"] == "improvement"
    assert exp.rollback_recommended is False and exp.evaluation_mode == "offline"
    assert exp.recommendation["recommendation"] == "promote" and exp.recommendation["by"] == "Society_Evaluator"
    mem = db.query(MemoryItem).filter(MemoryItem.source_type == "experiment", MemoryItem.source_id == exp.id).one()
    assert mem.validation_state == "validated" and mem.correlation_id == correlation
    assert db.query(MemoryItem).filter(MemoryItem.author_agent_id == report.agents["evaluator"]).count() >= 1

    # ── traceability: every event chains back to the anomaly ──
    events = db.query(SocietyEvent).filter(SocietyEvent.correlation_id == correlation).order_by(SocietyEvent.created_at).all()
    types = [e.event_type for e in events]
    for expected in (EventType.PROPOSAL_CREATED, EventType.PROPOSAL_APPROVED, EventType.REPO_READ_RESULT, EventType.CODE_CHANGE_REQUESTED, EventType.CODE_CANDIDATE_BUILT, EventType.CODE_CANDIDATE_QA_PASSED, EventType.CODE_CANDIDATE_SECURITY_REVIEW, EventType.CODE_CANDIDATE_READY, EventType.PROMOTION_REQUESTED, EventType.PROMOTION_BRANCH_READY, EventType.PROMOTION_PR_OPEN, EventType.PROMOTION_CI_PASSED, EventType.EXPERIMENT_REQUESTED, EventType.EXPERIMENT_FINISHED, EventType.PROMOTION_AWAITING_APPROVAL):
        assert expected in types, (expected, types)
    ids = {e.id for e in events}
    for e in events:
        if e.id != ev.id and e.actor_type == "agent":
            assert e.causation_id in ids, e.event_type
    # escrow released only after READY
    from services.registry.app.models import TaskSession

    task = db.query(TaskSession).one()
    assert _ev(task.status) == "completed" and task.escrow_amount == 10


@pytest.mark.timeout(600)
def test_human_merge_is_observed_and_post_merge_regression_recommends_rollback(db, SessionLocal, code_settings, code_repo):
    report, correlation, ev, provider, _ = run_code_story(db, SessionLocal, code_settings)
    promo = db.query(CodePromotion).one()
    assert _ev(promo.status) == "awaiting_approval"
    # a human approves and merges in GitHub; the controller only OBSERVES it
    provider.approve(promo.external_pr_number, "human-reviewer")
    provider.human_merge(promo.external_pr_number, merged_sha="deadbeef00")
    worker = SocietyWorker(SessionLocal, settings=code_settings, model=ScriptedRoleModel(), worker_id="e2e-code-2", promotion_provider=provider, telemetry_enabled=False)
    asyncio.run(worker.run_until_idle(max_cycles=10))
    db.refresh(promo)
    assert _ev(promo.status) == "merged" and promo.merged_sha == "deadbeef00" and provider.merge_count == 0
    assert promo.previous_good_sha == promo.base_sha
    # a post-merge experiment that FAILS recommends rollback and never erases evidence
    from services.registry.app.society import fitness

    cand = db.query(CodeCandidate).one()
    exp, created = fitness.request_experiment(db, settings=code_settings, promotion=promo, candidate=cand, agent=None, causation=None, source_run_id=None)
    assert created is False, "same promotion + same sha -> the existing experiment is reused (idempotent)"
    first = db.query(ChangeExperiment).count()
    # post-merge: the known-good baseline is the MERGED state (the fix), the candidate is what runs now
    forced = ChangeExperiment(id=uuid.uuid4(), candidate_id=cand.id, promotion_id=promo.id, correlation_id=correlation, baseline_sha=cand.head_sha, candidate_sha=cand.head_sha, status="planned", criteria_snapshot=dict(fitness.TRUSTED_CRITERIA))
    db.add(forced)
    db.commit()
    # simulate a regression: break the candidate worktree's test AFTER merge (post-merge signal)
    from services.registry.app.society.engineering import workspace as ws_mod

    ws = ws_mod.ensure_workspace(code_settings, cand.id)
    (ws.path / "app" / "textutil.py").write_text((ws.path / "app" / "textutil.py").read_text().replace('"yes", "on", "y"', '"nope"'))
    ws_mod.commit_all(ws, "regress")
    forced.candidate_sha = ws_mod.head_sha(ws)
    db.commit()
    fitness.evaluate_offline(db, settings=code_settings, exp=forced, candidate=cand, promotion=promo)
    db.refresh(forced)
    assert forced.decision == "fail" and forced.rollback_recommended is True
    assert any(e.event_type == EventType.ROLLBACK_RECOMMENDED for e in db.query(SocietyEvent).filter(SocietyEvent.correlation_id == correlation).all())
    assert db.query(ChangeExperiment).count() == first + 1, "failure evidence is appended, never erased"
