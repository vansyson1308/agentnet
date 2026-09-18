"""Promotion controller: state machine, idempotency, provider faults, races,
crash recovery, main protection, merge eligibility, disabled provider."""

from __future__ import annotations

import threading
import uuid

import pytest

from services.registry.app.models import (
    Agent,
    AgentRun,
    ChangeExperiment,
    CodeCandidate,
    CodePromotion,
    PromotionStatus,
    SocietyEvent,
)
from services.registry.app.society import promotion as pm
from services.registry.app.society.engineering import workspace as ws_mod
from services.registry.app.society.intents import FileEdit
from services.registry.app.society.seed import seed_society

DOC = "docs/society/candidates/promo.md"
GOOD_DOC = "# Promo\n\n## Problem\n\np\n\n## Proposed change\n\nc\n\n## Evidence\n\ne\n\n## Verification\n\nv\n"


def _ev(v):
    return v.value if hasattr(v, "value") else v


def ready_candidate(db, settings, report, *, path=DOC, content=GOOD_DOC, kind="docs", security=True, title="promo"):
    cid = uuid.uuid4()
    ws = ws_mod.ensure_workspace(settings, cid)
    ws_mod.apply_edits(ws, [FileEdit(path=path, content=content)], allowed=[path])
    head = ws_mod.commit_all(ws, "candidate")
    diff_hash, diff_lines = ws_mod.diff_identity(ws)
    cand = CodeCandidate(
        id=cid,
        correlation_id=uuid.uuid4(),
        title=title,
        spec={"files_allowed": [path], "acceptance_tests": ["tests/society/acceptance/test_candidate_docs.py"], "kind": kind},
        status="ready",
        branch_name=ws.branch,
        workspace_path=str(ws.path),
        base_sha=ws.base_sha,
        head_sha=head,
        changed_files=ws_mod.changed_files(ws),
        diff_hash=diff_hash,
        diff_lines=diff_lines,
        requested_by_agent_id=report.agents["architect"],
        builder_agent_id=report.agents["builder"],
        qa_agent_id=report.agents["qa"],
        security_agent_id=report.agents["security"] if security else None,
        qa_report={"verdict": "pass", "head_sha": head, "attempts": 1, "evaluated_by": "Society_QA", "summary": "ok"},
        security_report={"verdict": "pass", "head_sha": head, "reviewed_by": "Society_Security", "findings": [], "static_findings": []} if security else {},
        requires_security_review=security,
    )
    db.add(cand)
    db.commit()
    return cand


def _request(db, settings, cand, report):
    agent = db.query(Agent).filter(Agent.id == report.agents["governor"]).first()
    run = AgentRun(id=uuid.uuid4(), agent_id=agent.id, event_id=_event(db, cand.correlation_id).id, role="governor", status="running", correlation_id=cand.correlation_id, context_summary={}, intents_count=0, cost_usd=0)
    db.add(run)
    db.commit()
    promo, created = pm.request_promotion(db, settings=settings, candidate=cand, agent=agent, run=run, causation=None, source_run_id=run.id)
    db.commit()
    return promo, created


def _event(db, correlation):
    ev = SocietyEvent(id=uuid.uuid4(), event_type="t.promo", correlation_id=correlation, payload={}, status="processed")
    db.add(ev)
    db.commit()
    return ev


def drive(db_factory, settings, provider, *, worker_id="w-promo", rounds=12):
    results = []
    for _ in range(rounds):
        stats = pm.process_promotions(db_factory, settings=settings, provider=provider, worker_id=worker_id)
        if not stats:
            break
        results.append(stats)
    return results


def _experiment_pass(db, promo, cand):
    exp = ChangeExperiment(id=uuid.uuid4(), candidate_id=cand.id, promotion_id=promo.id, correlation_id=cand.correlation_id, baseline_sha=cand.base_sha, candidate_sha=cand.head_sha, status="pass", decision="pass", confidence="high", criteria_snapshot={"version": "fitness-v1"})
    db.add(exp)
    db.commit()
    return exp


# ── happy path + idempotency ───────────────────────────────────────────


def test_shadow_promotion_reaches_awaiting_approval_without_merging(db, db_factory, society_settings, temp_repo, monkeypatch):
    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report)
    promo, created = _request(db, society_settings, cand, report)
    assert created and _ev(promo.status) == "requested"
    again, created2 = _request(db, society_settings, cand, report)
    assert not created2 and again.id == promo.id, "same candidate -> same promotion"
    provider = pm.FakePromotionProvider()
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    assert _ev(promo.status) == "ci_passed" or _ev(promo.status) == "awaiting_approval"
    _experiment_pass(db, promo, cand)
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    assert _ev(promo.status) == "awaiting_approval", promo.failure_reason
    assert provider.publish_count == 1 and provider.pr_create_count == 1 and provider.merge_count == 0
    assert promo.external_branch == cand.branch_name and promo.external_pr_number == 101
    assert promo.risk_tier == "green"
    assert promo.eligibility["fitness_precheck"] is True and promo.eligibility["blocking"] == ["human_approval_satisfied"]
    types = [e.event_type for e in db.query(SocietyEvent).filter(SocietyEvent.correlation_id == cand.correlation_id).order_by(SocietyEvent.created_at).all()]
    assert types.index("promotion.branch_ready") < types.index("promotion.pr_open") < types.index("promotion.ci_passed") < types.index("promotion.awaiting_approval")
    # driving again is a no-op: no new branch/PR, no state churn
    drive(db_factory, society_settings, provider)
    assert provider.publish_count == 1 and provider.pr_create_count == 1
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == "promotion.pr_open").count() == 1


def test_auto_merge_off_by_default_even_for_green_with_approval(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report)
    promo, _ = _request(db, society_settings, cand, report)
    provider = pm.FakePromotionProvider()
    drive(db_factory, society_settings, provider)
    _experiment_pass(db, promo, cand)
    provider.approve(101, "human")
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    assert _ev(promo.status) == "merge_eligible"
    assert promo.eligibility["merge_eligible"] is True and promo.eligibility["auto_merge_allowed"] is False
    assert provider.merge_count == 0 and society_settings.auto_merge_enabled is False


def test_green_auto_merge_only_with_flag_and_all_gates(db, db_factory, temp_repo, tmp_path, monkeypatch):
    from services.registry.app.society.config import SocietySettings, reset_settings_cache

    monkeypatch.setenv("SOCIETY_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("SOCIETY_AUTONOMOUS_CODE_ENABLED", "true")
    monkeypatch.setenv("SOCIETY_REPO_ROOT", str(temp_repo))
    monkeypatch.setenv("SOCIETY_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("SOCIETY_PROMOTION_POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("SOCIETY_PROMOTION_PROVIDER", "fake")
    monkeypatch.setenv("SOCIETY_AUTO_MERGE_ENABLED", "true")
    reset_settings_cache()
    settings = SocietySettings()
    report = seed_society(db)
    cand = ready_candidate(db, settings, report)
    promo, _ = _request(db, settings, cand, report)
    provider = pm.FakePromotionProvider()
    drive(db_factory, settings, provider)
    db.refresh(promo)
    assert _ev(promo.status) in ("ci_passed", "awaiting_approval") and provider.merge_count == 0, "no fitness result yet -> no merge"
    _experiment_pass(db, promo, cand)
    drive(db_factory, settings, provider)
    db.refresh(promo)
    assert _ev(promo.status) == "merged" and provider.merge_count == 1 and promo.merged_sha
    reset_settings_cache()


def test_red_never_auto_merges_even_with_flag(db, db_factory, temp_repo, tmp_path, monkeypatch):
    from services.registry.app.society.config import SocietySettings, reset_settings_cache

    monkeypatch.setenv("SOCIETY_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("SOCIETY_AUTONOMOUS_CODE_ENABLED", "true")
    monkeypatch.setenv("SOCIETY_REPO_ROOT", str(temp_repo))
    monkeypatch.setenv("SOCIETY_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("SOCIETY_PROMOTION_POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("SOCIETY_PROMOTION_PROVIDER", "fake")
    monkeypatch.setenv("SOCIETY_AUTO_MERGE_ENABLED", "true")
    reset_settings_cache()
    settings = SocietySettings()
    report = seed_society(db)
    cand = ready_candidate(db, settings, report, path="services/registry/app/society/policy.py", content="# red change\n", kind="code")
    promo, _ = _request(db, settings, cand, report)
    provider = pm.FakePromotionProvider()
    drive(db_factory, settings, provider)
    _experiment_pass(db, promo, cand)
    provider.approve(101, "human")
    drive(db_factory, settings, provider)
    db.refresh(promo)
    assert promo.risk_tier == "red"
    assert _ev(promo.status) == "merge_eligible" and provider.merge_count == 0
    assert promo.eligibility["auto_merge_allowed"] is False
    reset_settings_cache()


# ── validation refusals ────────────────────────────────────────────────


def test_validation_rejects_missing_security_qa_mismatch_and_never(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    # AMBER code candidate without Security PASS
    c1 = ready_candidate(db, society_settings, report, path="services/registry/app/util_x.py", content="X = 1\n", kind="code", security=False, title="amber-no-sec")
    p1, _ = _request(db, society_settings, c1, report)
    # QA verdict for an older head
    c2 = ready_candidate(db, society_settings, report, title="stale-qa")
    c2.qa_report = {**c2.qa_report, "head_sha": "0" * 40}
    db.commit()
    p2, _ = _request(db, society_settings, c2, report)
    # NEVER: skipped test
    c3 = ready_candidate(db, society_settings, report, path="tests/test_new.py", content="import pytest\n@pytest.mark.skip\ndef test_x():\n    assert 1\n", kind="code", title="never")
    p3, _ = _request(db, society_settings, c3, report)
    provider = pm.FakePromotionProvider()
    drive(db_factory, society_settings, provider)
    for p, needle in ((p1, "Security PASS"), (p2, "QA PASS"), (p3, "NEVER tier")):
        db.refresh(p)
        assert _ev(p.status) == "rejected" and needle in (p.failure_reason or ""), (p.failure_reason,)
    assert provider.publish_count == 0


def test_main_and_foreign_branches_are_refused_and_never_forced(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report)
    cand.branch_name = "main"
    db.commit()
    promo, _ = _request(db, society_settings, cand, report)
    provider = pm.FakePromotionProvider()
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    assert _ev(promo.status) == "rejected" and "protected base branch" in promo.failure_reason
    # a branch outside the autonomous prefix is refused too
    cand2 = ready_candidate(db, society_settings, report, title="foreign")
    cand2.branch_name = "feature/human-work"
    db.commit()
    promo2, _ = _request(db, society_settings, cand2, report)
    drive(db_factory, society_settings, provider)
    db.refresh(promo2)
    assert _ev(promo2.status) == "rejected" and "autonomous prefix" in promo2.failure_reason
    # existing remote branch with different history is a conflict (no force push)
    cand3 = ready_candidate(db, society_settings, report, title="conflict")
    provider.branches[cand3.branch_name] = "f" * 40
    promo3, _ = _request(db, society_settings, cand3, report)
    drive(db_factory, society_settings, provider)
    db.refresh(promo3)
    assert _ev(promo3.status) == "superseded" and "no force push" in promo3.failure_reason
    assert not [c for c in provider.calls if c[0] == "merge"]
    assert not hasattr(provider, "force_push")


def test_disabled_provider_blocks_externally_and_never_fakes_success(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report)
    promo, _ = _request(db, society_settings, cand, report)
    provider = pm.DisabledPromotionProvider()
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    assert _ev(promo.status) == "blocked_external" and promo.external_pr_number is None
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == "promotion.blocked_external").count() == 1
    # once a provider exists, the same record resumes (no duplicate promotion)
    fake = pm.FakePromotionProvider()
    drive(db_factory, society_settings, fake)
    db.refresh(promo)
    assert _ev(promo.status) in ("ci_passed", "awaiting_approval") and db.query(CodePromotion).count() == 1


# ── faults, races, crash recovery ─────────────────────────────────────


def test_transient_faults_retry_then_reject_after_bounded_attempts(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report)
    promo, _ = _request(db, society_settings, cand, report)
    provider = pm.FakePromotionProvider()
    provider.inject(pm.ProviderTransient("429 rate limited"))
    provider.inject(pm.ProviderTransient("502"))
    provider.inject(pm.ProviderTransient("timeout"))
    # leases: expire immediately so the retries happen in this test
    for _ in range(4):
        pm.process_promotions(db_factory, settings=society_settings, provider=provider, worker_id="w")
        with db_factory() as s:
            row = s.query(CodePromotion).filter(CodePromotion.id == promo.id).first()
            row.lease_expires_at = None
            s.commit()
    db.refresh(promo)
    assert _ev(promo.status) != "rejected"
    assert provider.publish_count == 1, "the branch was published exactly once after the faults cleared"
    # attempts beyond the bound reject
    cand2 = ready_candidate(db, society_settings, report, title="exhaust")
    promo2, _ = _request(db, society_settings, cand2, report)
    for _ in range(society_settings.promotion_max_attempts + 1):
        provider.inject(pm.ProviderTransient("always"))
    for _ in range(society_settings.promotion_max_attempts + 2):
        pm.process_promotions(db_factory, settings=society_settings, provider=provider, worker_id="w")
        with db_factory() as s:
            row = s.query(CodePromotion).filter(CodePromotion.id == promo2.id).first()
            row.lease_expires_at = None
            s.commit()
    db.refresh(promo2)
    assert _ev(promo2.status) == "rejected" and "kept failing" in promo2.failure_reason


def test_ci_failure_base_move_head_change_and_closed_pr(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    provider = pm.FakePromotionProvider()
    # CI failure
    c1 = ready_candidate(db, society_settings, report, title="ci-fail")
    p1, _ = _request(db, society_settings, c1, report)
    provider.set_ci(c1.branch_name, "failed")
    drive(db_factory, society_settings, provider)
    db.refresh(p1)
    assert _ev(p1.status) == "ci_failed"
    # base moved after CI passed -> stale, never eligible
    c2 = ready_candidate(db, society_settings, report, title="base-moved")
    p2, _ = _request(db, society_settings, c2, report)
    drive(db_factory, society_settings, provider)
    _experiment_pass(db, p2, c2)
    provider.approve(provider.prs[c2.branch_name]["number"], "human")
    provider.move_base("base0001")
    drive(db_factory, society_settings, provider)
    db.refresh(p2)
    assert _ev(p2.status) != "merge_eligible" and p2.merge_state == "stale"
    assert p2.eligibility.get("branch_up_to_date") is False
    # head changed on the PR -> superseded
    c3 = ready_candidate(db, society_settings, report, title="head-moved")
    p3, _ = _request(db, society_settings, c3, report)
    drive(db_factory, society_settings, provider)
    provider.prs[c3.branch_name]["head_sha"] = "e" * 40
    drive(db_factory, society_settings, provider)
    db.refresh(p3)
    assert _ev(p3.status) == "superseded"
    # PR closed without merge -> rejected
    c4 = ready_candidate(db, society_settings, report, title="closed")
    p4, _ = _request(db, society_settings, c4, report)
    drive(db_factory, society_settings, provider)
    provider.close(provider.prs[c4.branch_name]["number"])
    drive(db_factory, society_settings, provider)
    db.refresh(p4)
    assert _ev(p4.status) == "rejected"


def test_two_controllers_racing_never_duplicate_branch_or_pr(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report)
    _request(db, society_settings, cand, report)
    provider = pm.FakePromotionProvider()
    lock = threading.Lock()
    orig_publish = provider.publish_branch

    def slow_publish(*a, **k):
        with lock:
            return orig_publish(*a, **k)

    provider.publish_branch = slow_publish
    errors = []

    def run(wid):
        try:
            for _ in range(8):
                pm.process_promotions(db_factory, settings=society_settings, provider=provider, worker_id=wid)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(f"w{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
    assert provider.publish_count == 1 and provider.pr_create_count == 1
    assert db.query(CodePromotion).count() == 1
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == "promotion.pr_open").count() == 1


@pytest.mark.parametrize("crash_after", ["publish_branch", "open_or_update_pr", "get_pr_state"])
def test_crash_after_each_side_effect_converges_without_duplicates(db, db_factory, society_settings, temp_repo, crash_after):
    """The controller dies right AFTER the provider side effect but BEFORE the
    transition is persisted. The re-claimed step replays the idempotent
    provider call and converges to the same state with one branch/one PR."""
    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report)
    promo, _ = _request(db, society_settings, cand, report)
    provider = pm.FakePromotionProvider()
    original = getattr(provider, crash_after)
    state = {"crashed": False}

    def crashing(*a, **k):
        result = original(*a, **k)
        if not state["crashed"]:
            state["crashed"] = True
            raise RuntimeError("worker died after the side effect")
        return result

    setattr(provider, crash_after, crashing)
    for _ in range(10):
        pm.process_promotions(db_factory, settings=society_settings, provider=provider, worker_id="w")
        with db_factory() as s:
            row = s.query(CodePromotion).filter(CodePromotion.id == promo.id).first()
            row.lease_expires_at = None
            s.commit()
    assert state["crashed"]
    db.refresh(promo)
    assert _ev(promo.status) in ("ci_passed", "awaiting_approval"), (promo.status, promo.failure_reason)
    assert provider.publish_count == 1 and provider.pr_create_count == 1
    assert db.query(CodePromotion).count() == 1
    for et in ("promotion.branch_ready", "promotion.pr_open"):
        assert db.query(SocietyEvent).filter(SocietyEvent.event_type == et).count() == 1, et


# ── state machine ──────────────────────────────────────────────────────


def test_illegal_transitions_are_refused_and_terminal_states_are_final(db, society_settings, temp_repo):
    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report)
    promo = CodePromotion(id=uuid.uuid4(), candidate_id=cand.id, correlation_id=cand.correlation_id, risk_tier="green", provider="fake", status=PromotionStatus.MERGED, base_sha=cand.base_sha, candidate_sha=cand.head_sha)
    db.add(promo)
    db.commit()
    for target in (PromotionStatus.REQUESTED, PromotionStatus.PR_OPEN, PromotionStatus.AWAITING_APPROVAL, PromotionStatus.REJECTED):
        with pytest.raises(pm.IllegalTransition):
            pm._transition(db, promo, cand, target)
    assert _ev(promo.status) == "merged"
    promo2 = CodePromotion(id=uuid.uuid4(), candidate_id=cand.id, correlation_id=cand.correlation_id, risk_tier="green", provider="fake", status=PromotionStatus.REQUESTED, base_sha=cand.base_sha, candidate_sha=cand.head_sha)
    with pytest.raises(pm.IllegalTransition):
        pm._transition(db, promo2, cand, PromotionStatus.MERGED)  # cannot skip straight to merged
    for status, allowed in pm.TRANSITIONS.items():
        assert status not in allowed, "no self loops"
        if status in pm.TERMINAL_STATUSES:
            assert not allowed


def test_change_budget_limits_open_prs_and_daily_promotions(db, db_factory, temp_repo, tmp_path, monkeypatch):
    from services.registry.app.society.config import SocietySettings, reset_settings_cache

    monkeypatch.setenv("SOCIETY_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("SOCIETY_AUTONOMOUS_CODE_ENABLED", "true")
    monkeypatch.setenv("SOCIETY_REPO_ROOT", str(temp_repo))
    monkeypatch.setenv("SOCIETY_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("SOCIETY_PROMOTION_POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("SOCIETY_MAX_OPEN_AUTONOMOUS_PRS", "1")
    monkeypatch.setenv("SOCIETY_MAX_PROMOTIONS_PER_DAY", "2")
    reset_settings_cache()
    settings = SocietySettings()
    report = seed_society(db)
    provider = pm.FakePromotionProvider()
    c1 = ready_candidate(db, settings, report, title="one")
    _request(db, settings, c1, report)
    drive(db_factory, settings, provider)
    c2 = ready_candidate(db, settings, report, path="docs/society/candidates/two.md", title="two")
    with pytest.raises(ValueError, match="autonomous PRs already open"):
        _request(db, settings, c2, report)
    reset_settings_cache()


def test_waiting_states_are_polled_at_most_once_per_interval(db, db_factory, society_settings, temp_repo, monkeypatch):
    """Regression: an open PR waiting on a human was re-claimed on every worker
    cycle (the demo showed ~1100 controller steps for one promotion). Waiting
    states honour the poll interval; internal steps still advance at once."""
    from datetime import timedelta

    from services.registry.app.society.config import reset_settings_cache, SocietySettings
    from services.registry.app.society.events import utcnow

    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report)
    promo, _ = _request(db, society_settings, cand, report)
    provider = pm.FakePromotionProvider()
    drive(db_factory, society_settings, provider)
    _experiment_pass(db, promo, cand)
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    assert _ev(promo.status) == "awaiting_approval"

    monkeypatch.setenv("SOCIETY_PROMOTION_POLL_INTERVAL_SECONDS", "600")
    reset_settings_cache()
    slow = SocietySettings()
    assert slow.promotion_poll_interval_seconds == 600
    # freshly updated -> not claimable within the interval
    assert pm.process_promotions(db_factory, settings=slow, provider=provider, worker_id="w") == {}
    assert pm.claim_next_promotion(db, worker_id="w", lease_seconds=30, min_age_seconds=600) is None
    # once the interval has elapsed it is polled again (still no merge, no new PR)
    later = utcnow() + timedelta(seconds=601)
    claimed = pm.claim_next_promotion(db, worker_id="w", lease_seconds=30, now=later, min_age_seconds=600)
    assert claimed is not None and claimed.id == promo.id
    pm._release(db, claimed)
    # a brand-new request (internal step) is never delayed by the poll interval
    cand2 = ready_candidate(db, society_settings, report)
    promo2, _ = _request(db, society_settings, cand2, report)
    claimed2 = pm.claim_next_promotion(db, worker_id="w", lease_seconds=30, min_age_seconds=600)
    assert claimed2 is not None and claimed2.id == promo2.id
    pm._release(db, claimed2)
    assert provider.pr_create_count == 1 and provider.merge_count == 0
    reset_settings_cache()
