"""G1y (owner 2026-11-01) items 1, 3 and 5.

1. A candidate's NEW test lives next to what it judges (templates/static -> tests/dashboard/,
   sdk/ -> tests/sdk/, docs and *.md -> tests/docs/); tests/society/** only for a change to
   society code. Staging: 9 of 11 bot PRs were RED ONLY because their acceptance test sat in
   tests/society/** while the product change was a dashboard template or a README.
3. One piece of live work per product file and objective: the design gate refuses a duplicate,
   the work generator does not file one, and of two that already exist the LATER is superseded
   (candidate rejected, its bot PR closed with a structural reason, its own task refunded once).
5. ``SOCIETY_AUTO_MERGE_MAX_TIER=red`` is refused unless every judge is owner-only.
"""

from __future__ import annotations

import pathlib
import uuid

import pytest

from services.registry.app.models import CodeCandidate, CodePromotion, ImprovementProposal, PromotionStatus, SocietyEvent, TaskSession, Wallet, WalletOwnerType
from services.registry.app.society import dedup, risk, spec_gate, tickets
from services.registry.app.society import promotion as pm
from services.registry.app.society.config import SocietyConfigError, SocietySettings, reset_settings_cache
from services.registry.app.society.events import EventType
from services.registry.app.society.seed import seed_society

REPO = pathlib.Path(__file__).resolve().parents[2]
LANDING = "services/dashboard/app/templates/landing.html"
SDK_README = "sdk/python/README.md"


def _placement(files, first):
    return [p for p in spec_gate.check_files(REPO, files, list(first), list(first)) if p.startswith("test_placement")]


# ── 1. test placement ───────────────────────────────────────────────────


@pytest.mark.parametrize("files,test,ok", [
    ([LANDING], "tests/dashboard/test_landing_cta.py", True),
    ([LANDING], "tests/society/test_landing_cta.py", False),       # PR #143: RED only because of its own test
    ([LANDING], "tests/test_landing_cta.py", False),               # PR #150 style: not next to what it judges
    ([SDK_README], "tests/sdk/test_sdk_readme_methods.py", True),
    ([SDK_README], "tests/society/test_sdk_readme_methods.py", False),  # PR #145
    (["examples/echo_agent/README.md"], "tests/docs/test_echo_readme.py", True),
    (["examples/echo_agent/README.md"], "tests/society/test_echo_readme.py", False),  # PR #140
    (["docs/AGENT_DEVELOPMENT.md"], "tests/docs/test_agent_dev_doc.py", True),
    (["services/registry/app/society/funnel.py"], "tests/society/test_funnel_more.py", True),  # society code: society tests
    (["services/registry/app/maintenance/activities.py"], "tests/society/maintenance/test_truncation_retry.py", True),  # PR #130 is genuinely RED
])
def test_a_new_test_sits_next_to_what_it_judges(files, test, ok):
    problems = _placement(files, [test])
    assert (problems == []) is ok, problems
    if not ok:
        assert "next to what it judges" in problems[0] or "put it under" in problems[0]


def test_the_suggested_place_is_named_in_the_structural_reason():
    (p,) = _placement([LANDING], ["tests/society/test_landing_cta.py"])
    assert "tests/dashboard/" in p and "RED" in p
    assert spec_gate.test_root_for([LANDING, "services/dashboard/app/static/site.css"]) == "tests/dashboard/"
    assert spec_gate.test_root_for([LANDING, SDK_README]) == "", "mixed areas prescribe no single root"


def test_a_test_placed_right_keeps_the_pr_at_the_tier_of_its_product_change():
    """The tier of the bot PR is the tier of its product change: a template is GREEN, its test in
    tests/dashboard/ AMBER (any code), never RED; tests/society/** made the same change RED."""
    right = risk.assess([LANDING, "tests/dashboard/test_landing_cta.py"], "", spec_kind="code").tier.value
    wrong = risk.assess([LANDING, "tests/society/test_landing_cta.py"], "", spec_kind="code").tier.value
    product = risk.assess([LANDING], "", spec_kind="code").tier.value
    assert (right, wrong) == (product, "red")


def test_the_new_test_roots_exist_and_required_ci_collects_them():
    for root in ("tests/dashboard", "tests/sdk", "tests/docs"):
        assert (REPO / root).is_dir(), root
    import importlib.util

    spec = importlib.util.spec_from_file_location("ctd", REPO / "scripts/ci/check_test_discovery.py")
    ctd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ctd)
    assert all(r.startswith(ctd.REQUIRED_ROOTS) for r in ("tests/dashboard/x", "tests/sdk/x", "tests/docs/x"))


# ── 5. MAX_TIER=red needs owner-only judges ─────────────────────────────


def test_every_judge_is_owner_only_today():
    assert risk.judges_not_owner_only() == []


def test_max_tier_red_is_refused_while_a_judge_is_not_owner_only(monkeypatch, temp_repo, tmp_path):
    for k, v in {"SOCIETY_RUNTIME_ENABLED": "true", "SOCIETY_AUTONOMOUS_CODE_ENABLED": "true", "SOCIETY_REPO_ROOT": str(temp_repo), "SOCIETY_WORKSPACE_ROOT": str(tmp_path / "ws"),
                 "SOCIETY_PROMOTION_PROVIDER": "fake", "SOCIETY_AUTO_MERGE_ENABLED": "true", "SOCIETY_AUTO_MERGE_MAX_TIER": "red"}.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    assert SocietySettings().auto_merge_max_tier == "red"
    loose = tuple(p for p in risk.OWNER_MERGE_PATTERNS if p != "services/registry/app/society/spec_gate.py")
    monkeypatch.setattr(risk, "OWNER_MERGE_PATTERNS", loose)
    with pytest.raises(SocietyConfigError, match="spec_gate.py"):
        SocietySettings()
    monkeypatch.setenv("SOCIETY_AUTO_MERGE_MAX_TIER", "amber")
    assert SocietySettings().auto_merge_max_tier == "amber", "the amber ceiling holds whatever the judges are"
    reset_settings_cache()


# ── 3. dedup ────────────────────────────────────────────────────────────


def _ticket(db, title, objective="O2", files=()):
    prop = ImprovementProposal(id=uuid.uuid4(), source="audit", title=title, problem="p", proposed_change="c", status="APPROVED", target_scope="platform", importance=50)
    db.add(prop)
    db.flush()
    tid = tickets.create(db, proposal_id=prop.id, title=title, fields={"objective_id": objective, "metric_id": "m", "expected_effect": 1, "direction": "up", "proof": [f"p:{title}"]},
                         source="owner", role="governor", importance=50)
    db.commit()
    return prop, tid


def _candidate(db, report, prop, tid, path, *, status="ready", pr=None, task_id=None):
    cand = CodeCandidate(id=uuid.uuid4(), proposal_id=prop.id, correlation_id=uuid.uuid4(), requested_by_agent_id=report.agents["architect"], builder_agent_id=report.agents["builder"],
                         title=prop.title, spec={"files_allowed": [path], "acceptance_tests": ["tests/dashboard/test_x.py"], "kind": "code"}, status=status,
                         changed_files=[path, "tests/dashboard/test_x.py"], branch_name=f"agentnet-auto/{uuid.uuid4()}", task_id=task_id)
    db.add(cand)
    db.flush()
    tickets.set_status(db, tid, "ready" if status == "ready" else "building", candidate_id=cand.id)
    promo = None
    if pr is not None:
        promo = CodePromotion(id=uuid.uuid4(), candidate_id=cand.id, correlation_id=cand.correlation_id, risk_tier="amber", provider="fake", status=PromotionStatus.CI_PASSED,
                              external_pr_number=pr, external_branch=cand.branch_name, evidence={}, eligibility={})
        db.add(promo)
    db.commit()
    return cand, promo


def test_a_design_that_targets_a_file_already_in_flight_for_the_objective_is_refused(db):
    report = seed_society(db)
    p1, t1 = _ticket(db, "hire section one")
    _candidate(db, report, p1, t1, "services/dashboard/app/templates/marketplace.html", pr=150)
    _, t2 = _ticket(db, "hire section two")
    problems = dedup.conflicts(db, ["services/dashboard/app/templates/marketplace.html"], "O2", t2)
    assert len(problems) == 1 and problems[0].startswith("duplicate: services/dashboard/app/templates/marketplace.html") and "PR #150" in problems[0]
    assert dedup.conflicts(db, ["services/dashboard/app/templates/marketplace.html"], "O1", t2) == [], "another objective is other work"
    assert dedup.conflicts(db, ["services/dashboard/app/templates/register.html"], "O2", t2) == []
    assert dedup.conflicts(db, ["services/dashboard/app/templates/marketplace.html"], "O2", t1) == [], "its own ticket is not a duplicate"
    assert dedup.lever_taken(db, ["services/dashboard/app/templates/marketplace.html"], "O2") is True
    assert dedup.lever_taken(db, ["services/dashboard/app/templates/network.html"], "O2") is False


def test_of_two_duplicates_the_later_is_superseded_its_pr_closed_and_its_task_refunded_once(db, SessionLocal, code_settings, grants_with_no_cooldown):
    """Staging #150 / #151: the same marketplace.html section, both open."""
    from .test_builder_decline import _run_as

    report = seed_society(db)
    grants_with_no_cooldown()
    caller = db.query(Wallet).filter(Wallet.owner_type == WalletOwnerType.AGENT, Wallet.owner_id == report.agents["architect"]).first()
    caller.balance_credits = 50
    db.commit()
    _run_as(db, SessionLocal, code_settings, "architect", [{"type": "CREATE_TASK", "payload": {"callee_agent": "Society_Builder", "capability": "implement_change", "input": {"candidate_id": "x"}, "max_budget": 10}}])
    task = db.query(TaskSession).one()
    assert db.query(Wallet).filter(Wallet.id == caller.id).one().reserved_credits == 10

    path = "services/dashboard/app/templates/marketplace.html"
    p1, t1 = _ticket(db, "hire walkthrough")
    first, _ = _candidate(db, report, p1, t1, path, pr=150)
    p2, t2 = _ticket(db, "hire example")
    second, promo2 = _candidate(db, report, p2, t2, path, pr=151, task_id=task.id)
    provider = pm.FakePromotionProvider()
    provider.prs[second.branch_name] = {"number": 151, "closed": False, "merged": False}

    assert dedup.supersede_duplicates(db, provider) == 1
    db.expire_all()
    assert (db.get(CodeCandidate, first.id).status.value, db.get(CodeCandidate, second.id).status.value) == ("ready", "rejected")
    assert db.get(CodeCandidate, second.id).error.startswith(f"{dedup.REASON}: {path} for O2 is already in candidate {str(first.id)[:8]} (PR #150)")
    assert db.get(CodePromotion, promo2.id).status.value == "rejected"
    assert ("close_pr", 151) == provider.calls[-1][:2] and provider.calls[-1][2].startswith(f"Closed by the AgentNet promotion controller: {dedup.REASON}")
    assert provider.prs[second.branch_name]["closed"] is True
    assert tickets.for_proposal(db, p2.id)["status"] == "closed"
    assert db.get(TaskSession, task.id).status.value == "failed"
    w = db.query(Wallet).filter(Wallet.id == caller.id).one()
    assert (w.balance_credits, w.reserved_credits) == (50, 0), "the escrow is released exactly once"
    ev = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.CODE_CANDIDATE_REJECTED, SocietyEvent.subject_id == second.id).one()
    assert ev.payload["reason_code"] == dedup.REASON and ev.payload["superseded_by"] == str(first.id) and ev.payload["task_refunded"] is True

    # idempotent: nothing left to supersede, no second refund, no second event
    assert dedup.supersede_duplicates(db, provider) == 0
    w = db.query(Wallet).filter(Wallet.id == caller.id).one()
    assert (w.balance_credits, w.reserved_credits) == (50, 0)
    assert db.query(SocietyEvent).filter(SocietyEvent.subject_id == second.id, SocietyEvent.event_type == EventType.CODE_CANDIDATE_REJECTED).count() == 1


def test_dedup_moves_money_only_through_the_escrow_path():
    import inspect

    src = inspect.getsource(dedup)
    assert "task_service.fail_task_with_refund" in src
    for forbidden in ("balance_credits", "reserved_credits", "Wallet", "wallet."):
        assert forbidden not in src, forbidden


def test_a_duplicate_the_controller_can_merge_is_kept_over_an_earlier_owner_only_twin(db):
    """Staging: #149 (RED: its test sat in tests/society/) and #150 (AMBER) both edit marketplace.html.
    Closing #150 for the older #149 would trade work that can land for work that waits on the owner."""
    report = seed_society(db)
    path = "services/dashboard/app/templates/marketplace.html"
    p1, t1 = _ticket(db, "share preview")
    red, red_promo = _candidate(db, report, p1, t1, path, pr=149)
    red_promo.eligibility = {"owner_merge_reasons": ["risk_tier_red_above_amber"]}
    p2, t2 = _ticket(db, "hire example")
    amber, amber_promo = _candidate(db, report, p2, t2, path, pr=150)
    amber_promo.eligibility = {"owner_merge_reasons": []}
    db.commit()
    (kept, gone, shared), = dedup.duplicate_pairs(db)
    assert (kept["id"], gone["id"], shared) == (amber.id, red.id, [path])
