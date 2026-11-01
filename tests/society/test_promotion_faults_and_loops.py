"""Owner brief 2026-10-31: why READY candidates lost their promotion, and how
long a normal story is.

Staging: 7 READY rows whose promotion ENDED "rejected" while their PR was still
open -- "provider kept failing after 221-322 attempts: GitHub returned 500 for
GET .../check-runs" and "refused: GitHub error 422 for GET /pulls/132". The
attempt counter counts every poll, so ONE fault ended a PR that had been polled
for hours. And no promotion could ever merge: no experiment was ever requested,
so fitness_precheck blocked them all.
"""

from __future__ import annotations

import uuid

import pytest

from services.registry.app.models import AgentRun, ChangeExperiment, CodePromotion, SocietyEvent
from services.registry.app.society import promotion as pm
from services.registry.app.society.config import SocietySettings, reset_settings_cache
from services.registry.app.society.events import emit_event
from services.registry.app.society.runs import dispatch_pending_events
from services.registry.app.society.seed import seed_society

from .test_promotion import _automerge_settings, _ev, _experiment_pass, _request, drive, ready_candidate


@pytest.fixture(autouse=True)
def _reset_settings():
    yield
    reset_settings_cache()


def _open_pr(db, db_factory, settings, provider, report, title):
    cand = ready_candidate(db, settings, report, title=title)
    promo, _ = _request(db, settings, cand, report)
    drive(db_factory, settings, provider)
    db.refresh(promo)
    assert promo.external_pr_number
    return cand, promo


def _poll(db_factory, settings, provider, promo, times):
    """Poll the open PR ``times`` times (lease cleared so each poll claims it)."""
    for _ in range(times):
        db = db_factory()
        row = db.query(CodePromotion).filter(CodePromotion.id == promo.id).first()
        row.lease_expires_at = None
        db.commit()
        db.close()
        pm.process_promotions(db_factory, settings=settings, provider=provider, worker_id="w-poll", max_items=1)


def test_one_provider_fault_after_many_polls_does_not_end_an_open_pr(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    provider = pm.FakePromotionProvider()
    cand, promo = _open_pr(db, db_factory, society_settings, provider, report, "many-polls")
    _poll(db_factory, society_settings, provider, promo, society_settings.promotion_max_attempts + 3)
    db.refresh(promo)
    assert promo.attempt > society_settings.promotion_max_attempts, "attempt counts every poll"
    provider.inject(pm.ProviderTransient("GitHub returned 500 for GET /check-runs"))
    _poll(db_factory, society_settings, provider, promo, 1)
    db.refresh(promo)
    assert _ev(promo.status) != "rejected", promo.failure_reason
    assert promo.evidence[pm.TRANSIENT_KEY] == 1
    _poll(db_factory, society_settings, provider, promo, 1)
    db.refresh(promo)
    assert promo.evidence[pm.TRANSIENT_KEY] == 0, "a completed step ends the run of faults"


def test_an_open_pr_is_held_through_consecutive_faults_never_rejected(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    provider = pm.FakePromotionProvider()
    cand, promo = _open_pr(db, db_factory, society_settings, provider, report, "held")
    for _ in range(society_settings.promotion_max_attempts + 2):
        provider.inject(pm.ProviderTransient("GitHub returned 500"))
        _poll(db_factory, society_settings, provider, promo, 1)
    db.refresh(promo)
    assert _ev(promo.status) not in ("rejected", "superseded")
    assert promo.evidence[pm.TRANSIENT_KEY] == society_settings.promotion_max_attempts + 2
    assert "in a row" in promo.failure_reason


def test_without_a_pr_consecutive_faults_still_end_the_promotion(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    provider = pm.FakePromotionProvider()
    cand = ready_candidate(db, society_settings, report, title="no-pr")
    promo, _ = _request(db, society_settings, cand, report)
    for _ in range(society_settings.promotion_max_attempts):
        provider.inject(pm.ProviderTransient("git push timed out"))
        _poll(db_factory, society_settings, provider, promo, 1)
        db.refresh(promo)
        if _ev(promo.status) == "rejected":
            break
    assert _ev(promo.status) == "rejected"
    assert promo.failure_reason.startswith(pm.PROVIDER_FAULT_REASON)


def _faulted(db, promo, reason):
    promo.status = "rejected"
    promo.failure_reason = reason
    db.commit()


def test_a_faulted_promotion_of_a_ready_candidate_resumes_its_open_pr(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    provider = pm.FakePromotionProvider()
    cand, promo = _open_pr(db, db_factory, society_settings, provider, report, "revive")
    _faulted(db, promo, "provider kept failing after 322 attempts: GitHub returned 500 for GET /repos/o/r/commits/x/check-runs")
    drive(db_factory, society_settings, provider)
    rows = db.query(CodePromotion).filter(CodePromotion.candidate_id == cand.id).order_by(CodePromotion.created_at).all()
    assert len(rows) == 2
    new = rows[1]
    assert new.evidence["revived_from"] == str(promo.id) and new.evidence["revivals"] == 1
    assert new.external_pr_number == promo.external_pr_number and new.candidate_sha == promo.candidate_sha
    assert new.requested_by_agent_id == promo.requested_by_agent_id, "the Governor's request, not a new one"
    assert _ev(new.status) not in ("rejected", "superseded")
    assert provider.pr_create_count == 1 and provider.publish_count == 1, "the same PR, no new branch or PR"
    # idempotent: a second sweep does not create a third row
    drive(db_factory, society_settings, provider)
    assert db.query(CodePromotion).filter(CodePromotion.candidate_id == cand.id).count() == 2


def test_a_revived_promotion_records_the_owners_merge(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    provider = pm.FakePromotionProvider()
    cand, promo = _open_pr(db, db_factory, society_settings, provider, report, "revive-merged")
    _faulted(db, promo, "refused: GitHub error 422 for GET /repos/o/r/pulls/132")
    provider.human_merge(promo.external_pr_number, "owner-merge")
    drive(db_factory, society_settings, provider)
    new = db.query(CodePromotion).filter(CodePromotion.candidate_id == cand.id).order_by(CodePromotion.created_at.desc()).first()
    db.refresh(cand)
    assert _ev(new.status) == "merged" and _ev(cand.status) == "merged"


@pytest.mark.parametrize("reason", ["closed_by_owner: pull request closed without merge", "refused: GitHub refused PUT /x (403)", "NEVER tier: test skipped"])
def test_decisions_are_never_revived(db, db_factory, society_settings, temp_repo, reason):
    report = seed_society(db)
    provider = pm.FakePromotionProvider()
    cand, promo = _open_pr(db, db_factory, society_settings, provider, report, "decided")
    _faulted(db, promo, reason)
    assert pm.revive_faulted_promotions(db) == 0


def test_revivals_are_bounded(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    provider = pm.FakePromotionProvider()
    cand, promo = _open_pr(db, db_factory, society_settings, provider, report, "bounded")
    _faulted(db, promo, "provider kept failing after 9 attempts: 500")
    for _ in range(pm.MAX_REVIVALS + 2):
        assert pm.revive_faulted_promotions(db) <= 1
        latest = db.query(CodePromotion).filter(CodePromotion.candidate_id == cand.id).order_by(CodePromotion.created_at.desc()).first()
        if _ev(latest.status) != "rejected":
            _faulted(db, latest, "provider kept failing after 9 attempts: 500")
    assert db.query(CodePromotion).filter(CodePromotion.candidate_id == cand.id).count() == pm.MAX_REVIVALS + 1


def test_the_controller_requests_the_fitness_evaluation_when_ci_is_green(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    provider = pm.FakePromotionProvider()
    cand, promo = _open_pr(db, db_factory, society_settings, provider, report, "fitness")
    exps = db.query(ChangeExperiment).filter(ChangeExperiment.promotion_id == promo.id).all()
    assert len(exps) == 1 and _ev(exps[0].status) == "planned" and exps[0].candidate_sha == cand.head_sha
    drive(db_factory, society_settings, provider)
    assert db.query(ChangeExperiment).filter(ChangeExperiment.promotion_id == promo.id).count() == 1, "once per head"
    req = db.query(SocietyEvent).filter(SocietyEvent.event_type == "experiment.requested", SocietyEvent.correlation_id == cand.correlation_id).one()
    assert req.actor_type == "system"


def test_no_experiment_before_ci_is_green(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    provider = pm.FakePromotionProvider()
    cand = ready_candidate(db, society_settings, report, title="red-no-fitness")
    provider.set_ci(cand.branch_name, "failed")
    promo, _ = _request(db, society_settings, cand, report)
    drive(db_factory, society_settings, provider)
    assert db.query(ChangeExperiment).filter(ChangeExperiment.promotion_id == promo.id).count() == 0


# ── GitHub provider classification ─────────────────────────────────────


def _gh(monkeypatch, transport, git_runner=None):
    from services.registry.app.society.promotion_github import GitHubPromotionProvider

    for k, v in {"SOCIETY_GITHUB_REPOSITORY": "owner/repo", "SOCIETY_PROMOTION_PROVIDER": "github",
                 "SOCIETY_GITHUB_CREDENTIAL_PROVIDER": "static", "SOCIETY_GITHUB_TOKEN": "t"}.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    creds = type("C", (), {"get": lambda self: type("T", (), {"token": "t", "username": "x-access-token"})(), "invalidate": lambda self: None})()
    return GitHubPromotionProvider(SocietySettings(), credentials=creds, transport=transport, git_runner=git_runner)


@pytest.mark.parametrize("status", [409, 422])
def test_a_4xx_on_a_read_is_transient_not_a_refusal(monkeypatch, status):
    p = _gh(monkeypatch, lambda m, u, j: (status, {"message": "x"}))
    with pytest.raises(pm.ProviderTransient):
        p._request("GET", "/repos/owner/repo/pulls/132")
    with pytest.raises(pm.ProviderRefused):
        p._request("PUT", "/repos/owner/repo/pulls/132/merge", json={})


def test_a_push_the_remote_failed_is_transient_not_a_conflict(monkeypatch, tmp_path):
    import subprocess

    from .test_github_credentials_and_push import _cand, _ws

    def runner(argv, cwd, env, timeout):
        return subprocess.CompletedProcess(argv, 1, "", "remote: Internal Server Error\n! [remote rejected] HEAD -> agentnet-auto/x (Internal Server Error)")

    p = _gh(monkeypatch, lambda m, u, j: (404, {}), git_runner=runner)
    with pytest.raises(pm.ProviderTransient):
        p.publish_branch(None, _cand(), _ws(tmp_path))


# ── loop breaker: length is not a loop, repetition is ──────────────────


def _settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    return SocietySettings()


def test_the_same_event_on_the_same_subject_is_cut_after_the_cap(db, monkeypatch):
    settings = _settings(monkeypatch, SOCIETY_MAX_RUNS_PER_CORRELATION="100", SOCIETY_MAX_SAME_WAKES_PER_CORRELATION="3")
    seed_society(db)
    routing = {"t.ping": ["scout"]}
    corr, subject = uuid.uuid4(), uuid.uuid4()
    for i in range(4):
        ev = emit_event(db, event_type="t.ping", correlation_id=corr, subject_type="code_candidate", subject_id=subject, idempotency_key=f"ping-{i}")
        db.commit()
        stats = dispatch_pending_events(db, settings=settings, routing=routing)
        db.refresh(ev)
        if i < 3:
            assert stats.loop_breaks == 0 and _ev(ev.status) != "ignored"
        else:
            assert stats.loop_breaks == 1 and _ev(ev.status) == "ignored" and "same subject" in ev.dispatch_note
    assert db.query(AgentRun).filter(AgentRun.correlation_id == corr).count() == 3


def test_a_long_story_of_distinct_steps_is_not_a_loop(db, monkeypatch):
    settings = _settings(monkeypatch, SOCIETY_MAX_RUNS_PER_CORRELATION="100", SOCIETY_MAX_SAME_WAKES_PER_CORRELATION="2")
    seed_society(db)
    steps = ["company.ticket_approved", "design.proposed", "design.review", "design.decided", "code_change.requested", "code_candidate.built",
             "code_candidate.qa_requested", "code_candidate.qa_passed", "code_candidate.security_passed", "code_candidate.ready",
             "promotion.pr_open", "promotion.ci_passed", "promotion.awaiting_approval"]
    routing = {s: ["scout", "governor"] for s in steps}
    corr, subject = uuid.uuid4(), uuid.uuid4()
    for i, step in enumerate(steps):
        emit_event(db, event_type=step, correlation_id=corr, subject_type="code_candidate", subject_id=subject, idempotency_key=f"s-{i}")
        db.commit()
        stats = dispatch_pending_events(db, settings=settings, routing=routing)
        assert stats.loop_breaks == 0, step
    assert db.query(AgentRun).filter(AgentRun.correlation_id == corr).count() == 2 * len(steps)


def test_the_validator_proposes_a_story_cap_from_measured_runs():
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location("p5_story", pathlib.Path(__file__).resolve().parents[2] / "deploy" / "railway" / "phase5_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.story_run_stats([]) == {"n": 0, "p50": None, "p95": None, "max": None, "proposed_cap": None}
    st = mod.story_run_stats([14, 16, 15, 18, 22, 17, 16, 19, 15, 20])
    assert st["n"] == 10 and st["max"] == 22 and st["p95"] == 22
    assert st["proposed_cap"] == 33, "p95 x 1.5, at least p95 + 6"
    assert mod.story_run_stats([4])["proposed_cap"] == 10
