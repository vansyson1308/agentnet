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
    # base moved after CI passed -> reconciled, and CI must run again before any
    # gate can pass. The promotion is NEVER eligible on the stale head.
    c2 = ready_candidate(db, society_settings, report, title="base-moved")
    p2, _ = _request(db, society_settings, c2, report)
    drive(db_factory, society_settings, provider)
    _experiment_pass(db, p2, c2)
    provider.approve(provider.prs[c2.branch_name]["number"], "human")
    stale_head = provider.prs[c2.branch_name]["head_sha"]
    provider.move_base("base0001")
    drive(db_factory, society_settings, provider)   # drives to quiescence
    db.refresh(p2)
    assert provider.update_branch_count == 1, "the base was merged INTO the head exactly once"
    assert "update_branch" in [c[0] for c in provider.calls]
    assert provider.merge_count == 0, "reconciling must never merge the PR"
    assert provider.prs[c2.branch_name]["head_sha"] != stale_head, "the head is a new merge commit, not a rewrite"
    assert p2.candidate_sha == provider.prs[c2.branch_name]["head_sha"], "the reconciled head is now the validated sha"
    assert p2.evidence["base_reconciles"] == 1 and p2.evidence["base_reconciled_from"] == stale_head[:40]
    assert p2.eligibility.get("branch_up_to_date") is True
    # The durable order is the real proof: the promotion went BACK to ci_pending
    # when the base moved, and only became eligible again afterwards -- it was
    # never eligible on the stale head.
    seq = [e.event_type for e in db.query(SocietyEvent).filter(SocietyEvent.correlation_id == c2.correlation_id).order_by(SocietyEvent.created_at, SocietyEvent.id).all()]
    assert "promotion.ci_pending" in seq, seq
    assert seq.index("promotion.ci_passed") < seq.index("promotion.ci_pending"), "ci_pending here is the RE-run after reconciling"
    if "promotion.merge_eligible" in seq:
        assert seq.index("promotion.ci_pending") < seq.index("promotion.merge_eligible"), "never eligible before the reconciled head passed CI"
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
        if status in pm.FINAL_STATUSES:
            assert not allowed, f"{status} must be final"
    # SUPERSEDED is terminal for every actor EXCEPT the controller proving the
    # head is its own update-branch merge commit, so it has exactly one edge.
    assert pm.TRANSITIONS[PromotionStatus.SUPERSEDED] == {PromotionStatus.CI_PENDING}
    assert pm.FINAL_STATUSES == (PromotionStatus.MERGED, PromotionStatus.REJECTED)


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


# ── fitness "no objection", draft lifecycle, bounded reconcile ─────────


def _experiment(db, promo, cand, *, status, decision, confidence="high", gates=None, deltas=None):
    exp = ChangeExperiment(
        id=uuid.uuid4(), candidate_id=cand.id, promotion_id=promo.id, correlation_id=cand.correlation_id,
        baseline_sha=cand.base_sha, candidate_sha=cand.head_sha, status=status, decision=decision,
        confidence=confidence, criteria_snapshot={"version": "fitness-v1"},
        hard_gate_results=gates if gates is not None else [{"gate": "no_test_regression", "passed": True}],
        metric_deltas=deltas or {},
    )
    db.add(exp)
    db.commit()
    return exp


def test_a_docs_candidate_can_never_improve_a_metric_so_inconclusive_must_not_block_forever(db, db_factory, society_settings, temp_repo):
    """fitness.py only says "pass" when a metric IMPROVED. A documentation
    change moves no metric, so under an `exp_status == PASS` test it can never
    qualify -- the live promotion of b8cee13c passed every hard gate, regressed
    nothing, and was blocked with no action that could ever clear it."""
    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report, title="inconclusive-ok")
    promo, _ = _request(db, society_settings, cand, report)
    provider = pm.FakePromotionProvider()
    drive(db_factory, society_settings, provider)
    _experiment(db, promo, cand, status="inconclusive", decision="inconclusive",
                gates=[{"gate": "no_test_regression", "passed": True}, {"gate": "no_security_regression", "passed": True}],
                deltas={"task_failure_rate": {"verdict": "neutral"}})
    provider.approve(provider.prs[cand.branch_name]["number"], "human")
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    assert promo.eligibility["fitness_precheck"] is True
    assert promo.eligibility["fitness_status"] == "inconclusive"
    assert _ev(promo.status) == "merge_eligible", promo.eligibility.get("blocking")


@pytest.mark.parametrize(
    "status,decision,confidence,gates,deltas,why",
    [
        ("fail", "fail", "high", [{"gate": "no_test_regression", "passed": False}], {}, "a failed experiment"),
        ("inconclusive", "inconclusive", "low", [{"gate": "attempts", "passed": False}], {}, "attempt budget exhausted"),
        ("inconclusive", "inconclusive", "high", [{"gate": "no_test_regression", "passed": False}], {}, "a hard gate failed"),
        ("inconclusive", "inconclusive", "high", [], {}, "no gate was evaluated at all"),
        ("inconclusive", "inconclusive", "high", [{"gate": "no_test_regression", "passed": True}], {"latency_p95_ms": {"verdict": "regression"}}, "a metric regressed"),
    ],
)
def test_inconclusive_only_counts_when_it_actually_looked_and_found_nothing(db, db_factory, society_settings, temp_repo, status, decision, confidence, gates, deltas, why):
    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report, title=f"blocked-{abs(hash(why)) % 9999}")
    promo, _ = _request(db, society_settings, cand, report)
    provider = pm.FakePromotionProvider()
    drive(db_factory, society_settings, provider)
    _experiment(db, promo, cand, status=status, decision=decision, confidence=confidence, gates=gates, deltas=deltas)
    provider.approve(provider.prs[cand.branch_name]["number"], "human")
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    assert promo.eligibility["fitness_precheck"] is False, why
    assert "fitness_precheck" in promo.eligibility["blocking"], why
    assert _ev(promo.status) != "merge_eligible" and provider.merge_count == 0


def test_a_missing_experiment_still_blocks():
    assert pm._fitness_satisfied(None) is False


def test_a_green_candidate_is_offered_for_review_and_the_draft_gate_is_real(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report, title="draft-green")
    promo, _ = _request(db, society_settings, cand, report)
    provider = pm.FakePromotionProvider()
    provider.set_ci(cand.branch_name, "pending")
    drive(db_factory, society_settings, provider)
    assert provider.prs[cand.branch_name]["draft"] is True, "the provider opens PRs as drafts"
    assert provider.ready_count == 0, "nothing is offered for review before its checks have passed"
    _experiment_pass(db, promo, cand)
    provider.set_ci(cand.branch_name, "passed")
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    assert provider.ready_count == 1 and provider.prs[cand.branch_name]["draft"] is False
    assert promo.evidence["ready_for_review_by"] == "promotion-controller"
    assert promo.eligibility["pr_not_draft"] is True
    types = [e.event_type for e in db.query(SocietyEvent).filter(SocietyEvent.correlation_id == cand.correlation_id).all()]
    assert "promotion.ready_for_review" in types
    # repeating is a no-op: one transition, one event
    drive(db_factory, society_settings, provider)
    assert provider.ready_count == 1
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == "promotion.ready_for_review").count() == 1


def test_a_red_candidate_is_never_taken_out_of_draft_by_the_controller(db, db_factory, society_settings, temp_repo):
    """A human may still take it out of draft and merge it -- that is RED's
    governance path. The CONTROLLER simply never does it for them."""
    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report, path="services/registry/app/society/policy.py", content="# red\n", kind="code", title="draft-red")
    promo, _ = _request(db, society_settings, cand, report)
    provider = pm.FakePromotionProvider()
    drive(db_factory, society_settings, provider)
    _experiment_pass(db, promo, cand)
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    assert promo.risk_tier == "red"
    assert provider.ready_count == 0 and provider.prs[cand.branch_name]["draft"] is True
    assert "risk_tier_red" in promo.evidence["ready_for_review_blocked_by"]
    assert promo.eligibility["auto_merge_allowed"] is False


def test_a_draft_pr_can_never_be_auto_merged(db, db_factory, temp_repo, tmp_path, monkeypatch):
    """The draft gate belongs to the auto-merge law (§23), and it is checked
    against the PR's real state, not against the controller's intention."""
    from services.registry.app.society.config import SocietySettings, reset_settings_cache

    for k, v in {"SOCIETY_RUNTIME_ENABLED": "true", "SOCIETY_AUTONOMOUS_CODE_ENABLED": "true", "SOCIETY_REPO_ROOT": str(temp_repo),
                 "SOCIETY_WORKSPACE_ROOT": str(tmp_path / "ws"), "SOCIETY_PROMOTION_POLL_INTERVAL_SECONDS": "0",
                 "SOCIETY_PROMOTION_PROVIDER": "fake", "SOCIETY_AUTO_MERGE_ENABLED": "true"}.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    settings = SocietySettings()
    report = seed_society(db)
    cand = ready_candidate(db, settings, report, title="draft-blocks-automerge")
    promo, _ = _request(db, settings, cand, report)
    provider = pm.FakePromotionProvider()
    # the provider refuses to leave draft, so the PR stays a draft throughout
    provider.mark_ready_for_review = lambda promotion, state: False
    drive(db_factory, settings, provider)
    _experiment_pass(db, promo, cand)
    drive(db_factory, settings, provider)
    db.refresh(promo)
    assert provider.prs[cand.branch_name]["draft"] is True
    assert promo.eligibility["pr_not_draft"] is False
    assert promo.eligibility["auto_merge_allowed"] is False
    assert provider.merge_count == 0 and _ev(promo.status) != "merged"
    reset_settings_cache()


def test_base_reconciliation_is_bounded_and_never_chases_a_moving_base(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report, title="moving-base")
    promo, _ = _request(db, society_settings, cand, report)
    provider = pm.FakePromotionProvider()
    drive(db_factory, society_settings, provider)
    _experiment_pass(db, promo, cand)
    for i in range(pm.MAX_BASE_RECONCILES + 3):
        provider.move_base(f"base{i:04d}")
        drive(db_factory, society_settings, provider)
    db.refresh(promo)
    assert provider.update_branch_count == pm.MAX_BASE_RECONCILES, "bounded, not an endless chase"
    assert "not chasing it further" in promo.evidence["base_reconcile_note"]
    assert provider.merge_count == 0


def test_reconciling_never_touches_a_head_someone_else_pushed(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report, title="foreign-push")
    promo, _ = _request(db, society_settings, cand, report)
    provider = pm.FakePromotionProvider()
    drive(db_factory, society_settings, provider)
    _experiment_pass(db, promo, cand)
    provider.prs[cand.branch_name]["head_sha"] = "f" * 40   # someone else pushed
    provider.move_base("base0002")
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    assert _ev(promo.status) == "superseded"
    assert provider.update_branch_count == 0, "a head we did not validate is never reconciled, it is abandoned"
    assert provider.merge_count == 0


# ── the GREEN-only autonomous merge law (§23-§27) ─────────────────────


def _automerge_settings(monkeypatch, temp_repo, tmp_path, **extra):
    from services.registry.app.society.config import SocietySettings, reset_settings_cache

    env = {"SOCIETY_RUNTIME_ENABLED": "true", "SOCIETY_AUTONOMOUS_CODE_ENABLED": "true",
           "SOCIETY_REPO_ROOT": str(temp_repo), "SOCIETY_WORKSPACE_ROOT": str(tmp_path / "ws"),
           "SOCIETY_PROMOTION_POLL_INTERVAL_SECONDS": "0", "SOCIETY_PROMOTION_PROVIDER": "fake",
           "SOCIETY_AUTO_MERGE_ENABLED": "true"}
    env.update(extra)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    return SocietySettings()


def test_the_historical_phase_guard_is_replaced_by_real_conditions_not_deleted(monkeypatch):
    """The old rule refused auto-merge with the GitHub provider outright. It is
    gone, but what replaced it must still refuse the unsafe combinations."""
    from services.registry.app.society.config import SocietyConfigError, SocietySettings, reset_settings_cache, validate_settings

    def settings_with(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        reset_settings_cache()
        return SocietySettings()

    base = {"SOCIETY_AUTO_MERGE_ENABLED": "true", "SOCIETY_AUTONOMOUS_CODE_ENABLED": "true",
            "SOCIETY_PROMOTION_PROVIDER": "github", "SOCIETY_GITHUB_REPOSITORY": "owner/repo",
            "SOCIETY_GITHUB_CREDENTIAL_PROVIDER": "static", "SOCIETY_GITHUB_TOKEN": "x", "ENVIRONMENT": "staging"}
    # the combination the old guard banned is now ALLOWED, because it is safe
    validate_settings(settings_with(**base))

    for bad, why in [
        ({"ENVIRONMENT": "production"}, "production"),
        ({"SOCIETY_PROMOTION_PROVIDER": "disabled"}, "pretend"),
        ({"SOCIETY_GITHUB_CREDENTIAL_PROVIDER": "disabled"}, "credential"),
        ({"SOCIETY_MAX_AUTONOMOUS_MERGES_PER_DAY": "0"}, "contradiction"),
        ({"SOCIETY_AUTONOMOUS_CODE_ENABLED": "false"}, "may not produce"),
    ]:
        with pytest.raises(SocietyConfigError) as exc:
            validate_settings(settings_with(**{**base, **bad}))
        assert why in str(exc.value), (bad, str(exc.value))
        for k in bad:
            monkeypatch.setenv(k, base.get(k, ""))
    reset_settings_cache()


def test_green_auto_merge_spends_the_daily_budget_and_then_freezes(db, db_factory, temp_repo, tmp_path, monkeypatch):
    settings = _automerge_settings(monkeypatch, temp_repo, tmp_path)
    assert settings.max_autonomous_merges_per_day == 1, "the default cap is one merge a day"
    report = seed_society(db)
    provider = pm.FakePromotionProvider()

    c1 = ready_candidate(db, settings, report, title="budget-1")
    p1, _ = _request(db, settings, c1, report)
    drive(db_factory, settings, provider)
    _experiment_pass(db, p1, c1)
    drive(db_factory, settings, provider)
    db.refresh(p1)
    assert _ev(p1.status) == "merged" and provider.merge_count == 1
    assert p1.evidence["auto_merged"] == "true", "the merge is recorded as autonomous so it spends the budget"

    # a second GREEN candidate the same day is frozen out
    c2 = ready_candidate(db, settings, report, title="budget-2")
    p2, _ = _request(db, settings, c2, report)
    drive(db_factory, settings, provider)
    _experiment_pass(db, p2, c2)
    drive(db_factory, settings, provider)
    db.refresh(p2)
    assert provider.merge_count == 1, "the daily cap held"
    assert _ev(p2.status) != "merged"
    assert any("daily_merge_cap_reached" in r for r in p2.eligibility["merge_freeze"])
    assert p2.eligibility["auto_merge_allowed"] is False
    from services.registry.app.society.config import reset_settings_cache
    reset_settings_cache()


def test_a_previous_autonomous_merge_under_failed_evaluation_freezes_the_next(db, db_factory, temp_repo, tmp_path, monkeypatch):
    settings = _automerge_settings(monkeypatch, temp_repo, tmp_path, SOCIETY_MAX_AUTONOMOUS_MERGES_PER_DAY="5")
    report = seed_society(db)
    provider = pm.FakePromotionProvider()
    c1 = ready_candidate(db, settings, report, title="regressed-1")
    p1, _ = _request(db, settings, c1, report)
    drive(db_factory, settings, provider)
    _experiment_pass(db, p1, c1)
    drive(db_factory, settings, provider)
    db.refresh(p1)
    assert _ev(p1.status) == "merged"
    # post-merge evaluation says roll it back
    exp = db.query(ChangeExperiment).filter(ChangeExperiment.promotion_id == p1.id).first()
    exp.rollback_recommended = True
    db.commit()

    c2 = ready_candidate(db, settings, report, title="regressed-2")
    p2, _ = _request(db, settings, c2, report)
    drive(db_factory, settings, provider)
    _experiment_pass(db, p2, c2)
    drive(db_factory, settings, provider)
    db.refresh(p2)
    assert provider.merge_count == 1, "merge authority froze after a bad merge"
    assert any("previous_autonomous_merge_under_failed_evaluation" in r for r in p2.eligibility["merge_freeze"])
    from services.registry.app.society.config import reset_settings_cache
    reset_settings_cache()


def test_the_provider_refuses_a_merge_the_controller_did_not_authorise(db, db_factory, temp_repo, tmp_path, monkeypatch):
    """Defense in depth (§24): an env var being true is not authority to land a
    commit on main. Calling the provider directly must fail closed."""
    settings = _automerge_settings(monkeypatch, temp_repo, tmp_path)
    report = seed_society(db)
    provider = pm.FakePromotionProvider()
    cand = ready_candidate(db, settings, report, title="direct-call")
    promo, _ = _request(db, settings, cand, report)
    drive(db_factory, settings, provider)
    db.refresh(promo)
    promo.eligibility = {**(promo.eligibility or {}), "auto_merge_allowed": False}
    db.commit()
    with pytest.raises(pm.ProviderRefused) as exc:
        provider.merge(promo, promo.candidate_sha or "x")
    assert "did not authorise" in str(exc.value)
    assert provider.merge_count == 0
    from services.registry.app.society.config import reset_settings_cache
    reset_settings_cache()


def test_the_github_provider_checks_the_persisted_verdict_not_just_the_flag(monkeypatch):
    from services.registry.app.society.config import SocietySettings, reset_settings_cache
    from services.registry.app.society.promotion_github import GitHubPromotionProvider

    # A GitHub provider with auto-merge on needs a real credential provider --
    # the config guard added in this change refuses the combination otherwise,
    # which is why this test has to configure one.
    for k, v in {"SOCIETY_GITHUB_REPOSITORY": "owner/repo", "SOCIETY_PROMOTION_PROVIDER": "github",
                 "SOCIETY_AUTO_MERGE_ENABLED": "true", "SOCIETY_GITHUB_CREDENTIAL_PROVIDER": "static",
                 "SOCIETY_GITHUB_TOKEN": "t", "SOCIETY_AUTONOMOUS_CODE_ENABLED": "true"}.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    s = SocietySettings()
    called = []
    p = GitHubPromotionProvider(s, credentials=type("C", (), {"get": lambda self: type("T", (), {"token": "t"})(), "invalidate": lambda self: None})(),
                                transport=lambda m, u, j: (called.append(u), (200, {"merged": True, "sha": "z"}))[1])

    def promo(**gates):
        return type("P", (), {"external_pr_number": 30, "external_branch": "agentnet-auto/x", "candidate_sha": "h", "risk_tier": "green", "eligibility": gates})()

    for gates, why in [
        ({}, "did not authorise"),
        ({"auto_merge_allowed": False, "trusted_risk_tier": "green"}, "did not authorise"),
        ({"auto_merge_allowed": True, "trusted_risk_tier": "red"}, "GREEN-only"),
        ({"auto_merge_allowed": True, "trusted_risk_tier": "green", "merge_freeze": ["daily_merge_cap_reached(1)"]}, "frozen"),
    ]:
        with pytest.raises(pm.ProviderRefused) as exc:
            p.merge(promo(**gates), "h")
        assert why in str(exc.value), (gates, str(exc.value))
    assert not called, "not one merge request reached GitHub"
    reset_settings_cache()


# ── the controller must recognise its own update-branch merge commit ──────
#
# GitHub answers PUT /pulls/{n}/update-branch with 202 Accepted and lands the
# merge commit asynchronously. The first real promotion (c6a8a473 / PR #30)
# read the head back immediately, saw the OLD sha, treated the reconcile as
# failed -- and then, when GitHub's merge commit appeared, classified ITS OWN
# COMMIT as a third-party push and superseded itself. These pin the repair.

def test_an_accepted_but_not_yet_visible_update_branch_is_not_a_failed_reconcile(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report, title="async-update-branch")
    promo, _ = _request(db, society_settings, cand, report)
    provider = pm.FakePromotionProvider()
    assert provider.async_update_branch is True, "the double must model GitHub's 202, not a synchronous API"
    drive(db_factory, society_settings, provider)
    _experiment_pass(db, promo, cand)
    db.refresh(promo)
    stale_head = promo.candidate_sha
    provider.move_base("base0001")
    drive(db_factory, society_settings, provider)
    db.refresh(promo)

    assert _ev(promo.status) != "superseded", "the controller destroyed its own reconcile"
    assert provider.update_branch_count == 1
    new_head = provider.prs[cand.branch_name]["head_sha"]
    assert new_head != stale_head
    assert promo.candidate_sha == new_head, "the merge commit GitHub made is now the validated sha"
    # Authorship was PROVED from the commit graph, not assumed from a flag.
    assert ("commit_parents", new_head) in provider.calls
    assert provider.parents[new_head][0] == stale_head, "update-branch merges the base INTO the head"
    assert "base_reconcile_pending" not in (promo.evidence or {}), "the request is closed once adopted"
    assert promo.evidence["base_reconciled_from"] == stale_head[:40]


def test_a_promotion_superseded_over_its_own_reconcile_recovers_from_the_commit_graph(db, db_factory, society_settings, temp_repo):
    """Exactly the state PR #30 was left in: the controller asked for
    update-branch, recorded only that the response was inconclusive, and then
    superseded itself when GitHub's merge commit appeared."""
    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report, title="superseded-over-own-reconcile")
    promo, _ = _request(db, society_settings, cand, report)
    provider = pm.FakePromotionProvider()
    drive(db_factory, society_settings, provider)
    _experiment_pass(db, promo, cand)
    db.refresh(promo)
    validated = promo.candidate_sha

    # Reproduce the damaged row: GitHub made the merge commit, the controller
    # recorded an inconclusive response and superseded on the next poll.
    merge_sha = "merged-by-github-0001"
    provider.prs[cand.branch_name]["head_sha"] = merge_sha
    provider.parents[merge_sha] = [validated, provider.base_sha]
    promo.evidence = {**(promo.evidence or {}), "base_reconcile_error": "update-branch did not move the head"}
    # Through the state machine, not by assignment: the damaged row really does
    # carry a promotion.superseded event, and the recovery must not erase it.
    pm._transition(db, promo, cand, PromotionStatus.SUPERSEDED, reason="PR head moved away from the validated candidate sha")
    db.commit()
    assert pm.active_promotion_for(db, cand.id) is not None, "a recoverable promotion still owns the PR"

    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    assert _ev(promo.status) != "superseded", "the promotion is alive again"
    assert promo.candidate_sha == merge_sha, "GitHub's merge commit is now the validated sha"
    assert provider.merge_count == 0, "recovering never merges anything"
    # The durable order is the proof, not the state drive() happens to stop in:
    # the supersede is still on the record, and the required checks ran AGAIN
    # afterwards, on the commit that would actually be merged.
    seq = [e.event_type for e in db.query(SocietyEvent).filter(SocietyEvent.correlation_id == cand.correlation_id).order_by(SocietyEvent.created_at, SocietyEvent.id).all()]
    assert "promotion.superseded" in seq, "recovery must not erase the supersede"
    after = seq[seq.index("promotion.superseded"):]
    assert "promotion.ci_pending" in after, after
    assert after.index("promotion.ci_pending") < after.index("promotion.ci_passed"), after
    # The second ci_passed is on a DIFFERENT commit, so it is its own event:
    # keyed by status alone it would be deduplicated away and the log could not
    # show that the required checks re-ran on the head that would be merged.
    passes = db.query(SocietyEvent).filter(SocietyEvent.subject_id == promo.id, SocietyEvent.event_type == "promotion.ci_passed").all()
    assert len(passes) == 2, "one pass per head, not one per promotion"
    assert {(e.payload or {}).get("candidate_sha") for e in passes} == {validated, merge_sha}


@pytest.mark.parametrize(
    "parents, why",
    [
        (["f" * 40, "base0000"], "a merge built on someone else's commit"),
        (["VALIDATED"], "a single-parent commit is a push, not update-branch"),
        (["base0000", "VALIDATED"], "our sha must be the FIRST parent"),
        ([], "an unknown commit proves nothing"),
    ],
)
def test_a_superseded_promotion_never_recovers_from_a_head_it_cannot_prove(db, db_factory, society_settings, temp_repo, parents, why):
    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report, title=f"foreign-{abs(hash(why)) % 9999}")
    promo, _ = _request(db, society_settings, cand, report)
    provider = pm.FakePromotionProvider()
    drive(db_factory, society_settings, provider)
    _experiment_pass(db, promo, cand)
    db.refresh(promo)
    validated = promo.candidate_sha

    foreign = "foreign-head-0001"
    provider.prs[cand.branch_name]["head_sha"] = foreign
    provider.parents[foreign] = [validated if p == "VALIDATED" else p for p in parents]
    promo.status = PromotionStatus.SUPERSEDED
    promo.evidence = {**(promo.evidence or {}), "base_reconcile_error": "inconclusive"}
    db.commit()

    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    assert _ev(promo.status) == "superseded", why
    assert promo.candidate_sha == validated, "an unproven head is never adopted"
    assert provider.merge_count == 0
    # Proved not ours -> the row stops being re-examined and releases the PR.
    assert "base_reconcile_error" not in (promo.evidence or {})
    assert promo.evidence["base_reconcile_recovery"]
    assert pm.active_promotion_for(db, cand.id) is None


def test_a_transient_commit_read_is_held_not_treated_as_a_foreign_push(db, db_factory, society_settings, temp_repo):
    """Destroying a promotion because one API read failed is the same bug one
    layer down, so the verdict is tri-state: proved / disproved / UNKNOWN.

    Driven directly, because injecting a fault into the loop hits whichever
    provider call happens to come first, not the one under test."""
    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report, title="transient-parents")
    promo, _ = _request(db, society_settings, cand, report)
    provider = pm.FakePromotionProvider()
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    validated = promo.candidate_sha
    merge_sha = "merged-by-github-0002"
    promo.evidence = {**(promo.evidence or {}), "base_reconcile_pending": {"from": validated, "attempt": 1}}
    db.commit()
    state = pm.PRState(ci="pending", head_sha=merge_sha)

    class Unreadable:
        def commit_parents(self, sha):
            raise pm.ProviderTransient("GitHub transport error")

    status_before = _ev(promo.status)
    assert pm._adopt_reconciled_head(db, provider=Unreadable(), promotion=promo, candidate=cand, state=state) is None
    assert _ev(promo.status) == status_before, "an unreadable commit changes nothing at all"
    assert promo.candidate_sha == validated
    assert (promo.evidence or {}).get("base_reconcile_pending"), "the request is still outstanding"

    class Readable:
        def commit_parents(self, sha):
            return [validated, "base0000"]

    assert pm._adopt_reconciled_head(db, provider=Readable(), promotion=promo, candidate=cand, state=state) is True
    db.commit()
    assert promo.candidate_sha == merge_sha, "once the read works, the same head is adopted"
    assert _ev(promo.status) == "ci_pending"
    assert promo.eligibility.get("ci_passed") is False, "the reconciled head has NOT passed CI yet"
    assert "base_reconcile_pending" not in (promo.evidence or {})
