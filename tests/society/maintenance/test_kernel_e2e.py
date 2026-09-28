"""End-to-end through the Maintenance OS with deterministic fakes.

A structural observation of a real class of defect (a raw structured value
rendered on a public page) becomes an incident, a repair case, a diagnosis,
an immutable plan, a patch authored through exact-text operations, a
deterministic QA pass, model reviews, a READY candidate, a PR through the
UNCHANGED promotion controller, a GREEN autonomous merge, an attested
release by the model-free Release Controller (exact SHA, production PR,
service-aware deploy, N healthy public observations) and finally the
incident's own monitor recovering -> AUTO_REPAIRED.

Scripted cognition and fake providers only: this proves the MECHANICS, not
model quality or a live release (NO FAKE AUTONOMY; docs/MAINTENANCE_LIVE_PROOF.md).
"""

from __future__ import annotations

import pytest

from services.registry.app.maintenance import incidents as inc_mod
from services.registry.app.maintenance import state_machine as sm
from services.registry.app.maintenance.orm import (
    MaintenanceKnowledge,
    MaintenanceKnownGood,
    MaintenanceRelease,
    RepairActivity,
    RepairArtifact,
    RepairCase,
    RepairPlanRevision,
    RepairTransition,
)
from services.registry.app.maintenance.release import Providers, ReleaseController, ReleaseSettings
from services.registry.app.maintenance.release_providers import FakeGitHub, FakePreview, FakeProbe, FakeRailway
from services.registry.app.society import promotion as pm

from .conftest import PAGE, VERIFY, at, drive_promotion, green_repair_script, raise_incident

pytestmark = pytest.mark.timeout(600)

GREEN_PATCH = f"--- a/{PAGE}\n+++ b/{PAGE}\n-  <li class=\"cap\">{{'name': 'echo', 'price': 1}}</li>\n+  <li class=\"cap\">echo</li>\n"


def _case(db):
    db.expire_all()
    return db.query(RepairCase).one()


def run(kernel, minutes, cycles=1):
    for i in range(cycles):
        kernel.reconcile(now=at(minutes + i * 0.01))


def fake_release_env(merged_sha: str, *, files=(PAGE,), patch=GREEN_PATCH):
    gh = FakeGitHub(main=["g0", "g1", merged_sha], production="g0", files_between={merged_sha: list(files)}, patches={merged_sha: patch})
    rw = FakeRailway()
    rw.seed("dashboard", "g0", "d-dash-good")
    rw.seed("registry", "g0", "d-reg-good")
    probe = FakeProbe([{"ui_root": True, "api_health": True, "marketplace": False}, {"ui_root": True, "api_health": True, "marketplace": True}])
    return gh, rw, probe


def release_controller(SessionLocal, mset, gh, rw, probe, preview=None):
    rs = ReleaseSettings(provider="fake", railway_service_ids={"dashboard": "s1", "registry": "s2"}, deploy_grace_seconds=0, require_signature=True)
    return ReleaseController(SessionLocal, providers=Providers(gh, rw, probe, preview or FakePreview()), settings=mset, release_settings=rs, worker_id="rc1")


def reach_releasing(db, SessionLocal, mset, sset, kernel_factory):
    from services.registry.app.society.seed import seed_society

    seed_society(db)
    raise_incident(db, mset)
    k = kernel_factory(green_repair_script())
    run(k, 2, cycles=8)
    provider = pm.FakePromotionProvider()
    for m in (20, 25, 30):
        drive_promotion(SessionLocal, sset, provider)
        run(k, m, cycles=2)
    return k, provider


def test_green_defect_is_repaired_released_and_verified_autonomously(db, SessionLocal, mset, sset, kernel_factory):
    k, provider = reach_releasing(db, SessionLocal, mset, sset, kernel_factory)
    case = _case(db)
    assert case.state == sm.CaseState.RELEASING.value, (case.state, case.terminal_reason)
    assert case.risk_class == "MAINTENANCE_GREEN" and provider.merge_count == 1
    plan = db.query(RepairPlanRevision).one()
    assert plan.files_allowed == [PAGE] and VERIFY in plan.acceptance_tests
    kinds = {a.kind for a in db.query(RepairActivity).all()}
    assert {"DiagnoseIncident", "DesignRepair", "AuthorPatch", "ReviewPatch", "SecurityReview"} <= kinds
    assert all(a.model_provider == "scripted" for a in db.query(RepairActivity).all()), "scripted runs are labelled as such"
    ver = db.query(RepairArtifact).filter(RepairArtifact.kind == "verification").one()
    assert ver.content["passed"] is True and ver.content["changed"] == [PAGE]

    rel = db.query(MaintenanceRelease).one()
    gh, rw, probe = fake_release_env(rel.head_sha)
    rc = release_controller(SessionLocal, mset, gh, rw, probe)
    for i in range(12):
        rc.run_once(now=at(40 + i * 2))
    db.expire_all()
    rel = db.query(MaintenanceRelease).one()
    assert rel.status == "succeeded", (rel.status, rel.failure_reason, rel.verification.get("history"))
    assert gh.merges == 1 and rel.pr_number and rel.release_branch == f"release/prod-{rel.head_sha[:12]}"
    assert rel.services == ["dashboard"], "only the changed service deploys"
    assert rw.deploy_calls == 1 and rel.deployments["dashboard"]["source"] == "triggered"
    assert db.query(MaintenanceKnownGood).count() == 2, "pre-release known-good + the new one"

    # healthy observations taken while a release is still settling never count
    inc_mod.observe_healthy(db, mset, target="production", desired_state_ref="marketplace", sli="public_pages", source="public_surface_monitor", collector_version="surface/1", now=at(64))
    db.commit()
    run(k, 65, cycles=1)
    assert _case(db).state == sm.CaseState.POST_RELEASE_VERIFYING.value
    # the incident's own monitor sees the page healthy on the released product: recovery streak
    for i in range(3):
        inc_mod.observe_healthy(db, mset, target="production", desired_state_ref="marketplace", sli="public_pages", source="public_surface_monitor", collector_version="surface/1", now=at(70 + i))
    db.commit()
    run(k, 75, cycles=3)
    case = _case(db)
    assert case.state == sm.CaseState.AUTO_REPAIRED.value, (case.state, case.terminal_reason)
    trail = [t.to_state for t in db.query(RepairTransition).order_by(RepairTransition.id).all()]
    assert trail == ["DETECTED", "CONFIRMED", "TRIAGED", "DIAGNOSING", "PLAN_READY", "BUILDING", "VERIFYING", "PROMOTING",
                     "READY_FOR_RELEASE", "RELEASING", "POST_RELEASE_VERIFYING", "AUTO_REPAIRED"]
    k_row = db.query(MaintenanceKnowledge).one()
    assert k_row.outcome == "AUTO_REPAIRED" and k_row.root_cause.startswith("[hypothesis")
    assert k_row.release_result == "succeeded"


def test_release_regression_rolls_back_to_known_good(db, SessionLocal, mset, sset, kernel_factory):
    k, _ = reach_releasing(db, SessionLocal, mset, sset, kernel_factory)
    rel = db.query(MaintenanceRelease).one()
    gh, rw, _ = fake_release_env(rel.head_sha)
    # baseline healthy, then the new release breaks the apex (controlled failure; preview/staging only)
    probe = FakeProbe([{"ui_root": True, "api_health": True}, {"ui_root": False, "api_health": True}, {"ui_root": True, "api_health": True}])
    rc = release_controller(SessionLocal, mset, gh, rw, probe)
    for i in range(16):
        rc.run_once(now=at(40 + i * 2))
    db.expire_all()
    rel = db.query(MaintenanceRelease).one()
    assert rel.status == "rolled_back", (rel.status, rel.failure_reason, rel.rollback)
    assert rel.rollback["services"]["dashboard"]["method"] == "rollback" and rel.rollback["services"]["dashboard"]["to"] == "d-dash-good"
    assert rel.rollback["result"] == "restored" and rel.rollback["reconciliation"]["pr"]
    from services.registry.app.maintenance.orm import MaintenanceReleaseFreeze

    fr = db.query(MaintenanceReleaseFreeze).filter(MaintenanceReleaseFreeze.reason_code == "rollback_parity").one()
    assert fr.lifted_at is None or fr.lift_reason
    run(k, 80, cycles=2)
    case = _case(db)
    assert case.state == sm.CaseState.AUTO_ROLLED_BACK.value, (case.state, case.terminal_reason)
