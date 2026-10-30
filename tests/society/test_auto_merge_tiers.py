"""Auto-merge beyond GREEN (SOCIETY_AUTO_MERGE_MAX_TIER) and the merge queue.

The owner asked for a normal team workflow: CI green + QA + Security -> merge,
then the other PRs update their branch and continue. These pin that:

* AMBER / RED merge on their own only when the ceiling allows it;
* the owner-only core (the merge law and the CI that judges it) never does;
* after a merge the next PR is brought up to date, re-checked and merged;
* a red PR on a stale base is updated too.
"""

from __future__ import annotations

import pytest

from services.registry.app.models import SocietyEvent
from services.registry.app.society import promotion as pm
from services.registry.app.society import risk
from services.registry.app.society.config import SocietyConfigError, SocietySettings, reset_settings_cache, validate_settings
from services.registry.app.society.seed import seed_society

from .test_promotion import _automerge_settings, _ev, _experiment_pass, _request, drive, ready_candidate

AMBER_PATH = "sdk/python/agentnet/hint.py"
RED_PATH = "services/payment/app/fee_note.py"
CORE_PATH = "services/registry/app/society/policy.py"


@pytest.fixture(autouse=True)
def _reset_settings():
    yield
    reset_settings_cache()


def _land(db, db_factory, settings, provider, report, *, path, title):
    cand = ready_candidate(db, settings, report, path=path, content=f"# {title}\n", kind="code", title=title)
    promo, _ = _request(db, settings, cand, report)
    drive(db_factory, settings, provider)
    _experiment_pass(db, promo, cand)
    drive(db_factory, settings, provider)
    db.refresh(promo)
    return cand, promo


def test_the_default_ceiling_is_green_so_amber_still_waits_for_the_owner(db, db_factory, temp_repo, tmp_path, monkeypatch):
    settings = _automerge_settings(monkeypatch, temp_repo, tmp_path)
    assert settings.auto_merge_max_tier == "green"
    provider = pm.FakePromotionProvider()
    _, promo = _land(db, db_factory, settings, provider, seed_society(db), path=AMBER_PATH, title="amber-default")
    assert promo.risk_tier == "amber"
    assert provider.merge_count == 0 and provider.ready_count == 0
    assert promo.eligibility["auto_merge_allowed"] is False
    assert "risk_tier_amber_above_green" in promo.eligibility["owner_merge_reasons"]


def test_amber_merges_on_green_ci_when_the_ceiling_allows_it(db, db_factory, temp_repo, tmp_path, monkeypatch):
    settings = _automerge_settings(monkeypatch, temp_repo, tmp_path, SOCIETY_AUTO_MERGE_MAX_TIER="amber")
    provider = pm.FakePromotionProvider()
    _, promo = _land(db, db_factory, settings, provider, seed_society(db), path=AMBER_PATH, title="amber-merge")
    assert promo.risk_tier == "amber"
    assert provider.ready_count == 1, "taken out of draft by the controller"
    assert _ev(promo.status) == "merged" and provider.merge_count == 1
    assert promo.evidence["auto_merged"] == "true" and promo.eligibility["owner_merge_reasons"] == []


def test_red_merges_on_green_ci_when_the_ceiling_is_red(db, db_factory, temp_repo, tmp_path, monkeypatch):
    settings = _automerge_settings(monkeypatch, temp_repo, tmp_path, SOCIETY_AUTO_MERGE_MAX_TIER="red")
    provider = pm.FakePromotionProvider()
    _, promo = _land(db, db_factory, settings, provider, seed_society(db), path=RED_PATH, title="red-merge")
    assert promo.risk_tier == "red"
    assert _ev(promo.status) == "merged" and provider.merge_count == 1


def test_red_waits_while_ci_is_red_and_merges_once_it_turns_green(db, db_factory, temp_repo, tmp_path, monkeypatch):
    settings = _automerge_settings(monkeypatch, temp_repo, tmp_path, SOCIETY_AUTO_MERGE_MAX_TIER="red")
    provider = pm.FakePromotionProvider()
    report = seed_society(db)
    cand = ready_candidate(db, settings, report, path=RED_PATH, content="# red-ci\n", kind="code", title="red-ci")
    provider.set_ci(cand.branch_name, "failed")
    promo, _ = _request(db, settings, cand, report)
    drive(db_factory, settings, provider)
    _experiment_pass(db, promo, cand)
    drive(db_factory, settings, provider)
    db.refresh(promo)
    assert _ev(promo.status) == "ci_failed" and provider.merge_count == 0
    provider.set_ci(cand.branch_name, "passed")
    drive(db_factory, settings, provider)
    db.refresh(promo)
    assert _ev(promo.status) == "merged" and provider.merge_count == 1


def test_the_owner_only_core_never_auto_merges_even_at_ceiling_red(db, db_factory, temp_repo, tmp_path, monkeypatch):
    settings = _automerge_settings(monkeypatch, temp_repo, tmp_path, SOCIETY_AUTO_MERGE_MAX_TIER="red")
    provider = pm.FakePromotionProvider()
    _, promo = _land(db, db_factory, settings, provider, seed_society(db), path=CORE_PATH, title="core")
    assert promo.risk_tier == "red"
    assert provider.merge_count == 0 and provider.ready_count == 0
    assert promo.eligibility["auto_merge_allowed"] is False and promo.eligibility["human_approval_required"] is True
    assert any(r.startswith("owner_merge_core:") for r in promo.eligibility["owner_merge_reasons"])


@pytest.mark.parametrize("path", [
    "services/registry/app/society/risk.py",
    "services/registry/app/society/promotion.py",
    "services/registry/app/society/fitness.py",
    "services/registry/app/society/config.py",
    ".github/workflows/ci.yml",
    "pytest.ini",
    "tests/society/test_auto_merge_tiers.py",
])
def test_the_merge_law_and_its_judges_are_owner_only(path):
    assert risk.owner_merge_paths([path]) == [path]


def test_ordinary_red_paths_are_not_owner_only():
    assert risk.owner_merge_paths([RED_PATH, "services/registry/migrations/versions/0031_x.py", "services/registry/app/task_service.py"]) == []


def test_the_merge_queue_updates_the_next_branch_and_merges_it_after_ci(db, db_factory, temp_repo, tmp_path, monkeypatch):
    settings = _automerge_settings(monkeypatch, temp_repo, tmp_path, SOCIETY_AUTO_MERGE_MAX_TIER="amber",
                                   SOCIETY_MAX_AUTONOMOUS_MERGES_PER_DAY="5")
    provider = pm.FakePromotionProvider()
    provider.async_update_branch = False
    report = seed_society(db)
    c1 = ready_candidate(db, settings, report, path=AMBER_PATH, content="# one\n", kind="code", title="queue-1")
    c2 = ready_candidate(db, settings, report, path="sdk/python/agentnet/other.py", content="# two\n", kind="code", title="queue-2")
    p1, _ = _request(db, settings, c1, report)
    p2, _ = _request(db, settings, c2, report)
    drive(db_factory, settings, provider, rounds=1)
    _experiment_pass(db, p1, c1)
    drive(db_factory, settings, provider)
    db.refresh(p1)
    db.refresh(p2)
    assert _ev(p1.status) == "merged" and provider.merge_count == 1
    assert _ev(p2.status) != "merged", "no fitness result for the second one yet"

    provider.move_base("main-after-queue-1")   # the first merge moved main
    _experiment_pass(db, p2, c2)
    drive(db_factory, settings, provider)
    db.refresh(p2)
    assert provider.update_branch_count == 1, "the next PR is brought up to date"
    assert _ev(p2.status) == "merged" and provider.merge_count == 2
    types = [e.event_type for e in db.query(SocietyEvent).filter(SocietyEvent.correlation_id == c2.correlation_id).order_by(SocietyEvent.created_at).all()]
    assert "promotion.ci_pending" in types, "required checks re-ran on the updated head before the merge"


def test_a_red_pr_on_a_stale_base_is_updated_so_its_checks_rerun(db, db_factory, society_settings, temp_repo):
    report = seed_society(db)
    cand = ready_candidate(db, society_settings, report, title="stale-red-ci")
    provider = pm.FakePromotionProvider()
    provider.set_ci(cand.branch_name, "failed")
    promo, _ = _request(db, society_settings, cand, report)
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    assert _ev(promo.status) == "ci_failed"
    provider.move_base("main-has-the-fix")
    provider.set_ci(cand.branch_name, "passed")   # the checks re-run on the updated head and pass
    _experiment_pass(db, promo, cand)
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    assert provider.update_branch_count == 1
    assert _ev(promo.status) == "awaiting_approval", "back in the queue (auto-merge is off in these settings)"


@pytest.mark.parametrize("value,ok", [("green", True), ("amber", True), ("red", True), ("never", False), ("bogus", False)])
def test_the_ceiling_is_validated(monkeypatch, value, ok):
    monkeypatch.setenv("SOCIETY_AUTO_MERGE_MAX_TIER", value)
    reset_settings_cache()
    if ok:
        assert not [p for p in validate_settings(SocietySettings()) if "SOCIETY_AUTO_MERGE_MAX_TIER" in p]
    else:
        with pytest.raises(SocietyConfigError, match="SOCIETY_AUTO_MERGE_MAX_TIER"):
            SocietySettings()


def test_the_github_provider_honours_the_ceiling_and_the_owner_core(monkeypatch):
    from services.registry.app.society.promotion_github import GitHubPromotionProvider

    for k, v in {"SOCIETY_GITHUB_REPOSITORY": "owner/repo", "SOCIETY_PROMOTION_PROVIDER": "github",
                 "SOCIETY_AUTO_MERGE_ENABLED": "true", "SOCIETY_GITHUB_CREDENTIAL_PROVIDER": "static",
                 "SOCIETY_GITHUB_TOKEN": "t", "SOCIETY_AUTONOMOUS_CODE_ENABLED": "true", "SOCIETY_AUTO_MERGE_MAX_TIER": "amber"}.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    called = []
    p = GitHubPromotionProvider(SocietySettings(), credentials=type("C", (), {"get": lambda self: type("T", (), {"token": "t"})(), "invalidate": lambda self: None})(),
                                transport=lambda m, u, j: (called.append(u), (200, {"merged": True, "sha": "z"}))[1])

    def promo(**gates):
        return type("P", (), {"external_pr_number": 30, "external_branch": "agentnet-auto/x", "candidate_sha": "h", "risk_tier": gates.get("trusted_risk_tier", "green"), "eligibility": gates})()

    with pytest.raises(pm.ProviderRefused, match="limited to green/amber"):
        p.merge(promo(auto_merge_allowed=True, trusted_risk_tier="red"), "h")
    with pytest.raises(pm.ProviderRefused, match="only the owner merges"):
        p.merge(promo(auto_merge_allowed=True, trusted_risk_tier="amber", owner_merge_reasons=["owner_merge_core:pytest.ini"]), "h")
    assert not called
    assert p.merge(promo(auto_merge_allowed=True, trusted_risk_tier="amber", owner_merge_reasons=[]), "h") == "z"
    assert len(called) == 1
