"""Kernel scenarios: AMBER/RED/constitutional, rescope, QA feedback, kill
switch, anti-busywork no-op, model failure classes. Scripted cognition only."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from services.registry.app.maintenance.activities import ActivityError, ModelReply, ScriptedActivityModel
from services.registry.app.maintenance.orm import MaintenanceKnowledge, MaintenanceRelease, RepairActivity, RepairAttempt, RepairCase, RepairPlanRevision
from services.registry.app.society import promotion as pm

from .conftest import MAIN, PAGE, VERIFY, at, drive_promotion, green_repair_script, raise_incident
from .test_kernel_e2e import fake_release_env, release_controller, run

pytestmark = pytest.mark.timeout(600)


def _case(db):
    db.expire_all()
    return db.query(RepairCase).one()


def base_script(overrides):
    """green_repair_script with per-activity overrides: kind -> callable(messages, n) -> action."""
    calls = {}
    green = green_repair_script()

    def script(messages):
        system = messages[0]["content"]
        kind = next((k for k in overrides if k in system), None)
        if kind is None:
            return green(messages)
        calls[kind] = calls.get(kind, 0) + 1
        return overrides[kind](messages, calls[kind])

    return script


def amber_patch(messages, n):
    turns = sum(1 for m in messages if m["role"] == "assistant")
    if turns == 0:
        return {"action": "apply_patch", "args": {"files": [
            {"path": PAGE, "operations": [{"op": "replace_exact", "old": "{'name': 'echo', 'price': 1}", "new": "echo"}]},
            {"path": MAIN, "operations": [{"op": "insert_after", "anchor": 'return "page.html"\n', "text": "\n\ndef marketplace_badges():\n    return ['echo']\n"}]},
        ]}}
    return {"action": "submit", "result": {"summary": "render names; expose badges"}}


def amber_design(messages, n):
    return {"action": "submit", "result": {"root_cause": "template renders raw objects", "approach": "render names in the template and a helper in the route module", "files_allowed": [PAGE, MAIN], "acceptance_tests": [VERIFY], "contract_refs": []}}


def test_amber_repair_is_escalated_complete_and_resumed_by_the_owner_merge(db, SessionLocal, mset, sset, kernel_factory):
    from services.registry.app.society.seed import seed_society

    seed_society(db)
    raise_incident(db, mset)
    k = kernel_factory(base_script({"DesignRepair": amber_design, "AuthorPatch": amber_patch}))
    run(k, 2, cycles=8)
    provider = pm.FakePromotionProvider()
    for m in (20, 25):
        drive_promotion(SessionLocal, sset, provider)
        run(k, m, cycles=2)
    case = _case(db)
    assert case.state == "SAFELY_ESCALATED" and case.resumable and case.terminal_reason == "owner_approval_required", (case.state, case.terminal_reason)
    assert case.risk_class == "AMBER" and provider.merge_count == 0
    pkg = case.escalation
    for key in ("incident", "root_cause", "changes", "tests", "qa", "security", "risk", "staging", "pr", "rollback_plan", "decision"):
        assert key in pkg, key
    assert pkg["qa"] == "pass" and pkg["security"] == "pass" and pkg["pr"]["number"]
    calls_before = db.query(RepairActivity).count()
    run(k, 40, cycles=5)
    assert db.query(RepairActivity).count() == calls_before, "no agent runs while waiting for the owner"
    # the owner's only action: merge the PR on GitHub
    provider.human_merge(pkg["pr"]["number"], merged_sha="owner-merge-1")
    drive_promotion(SessionLocal, sset, provider, rounds=2)
    run(k, 50, cycles=2)
    case = _case(db)
    assert case.state == "RELEASING" and case.facts["owner_approval"]["kind"] == "owner_merged_pr", (case.state, case.terminal_reason)
    assert db.query(RepairActivity).count() == calls_before, "approval resumed the persisted case; no model re-call"
    rel = db.query(MaintenanceRelease).one()
    gh, rw, probe = fake_release_env(rel.head_sha, files=(PAGE, MAIN), patch=f"+++ b/{MAIN}\n+def marketplace_badges():\n+    return ['echo']\n")
    rc = release_controller(SessionLocal, mset, gh, rw, probe)
    rc.run_once(now=at(60))
    db.expire_all()
    assert db.query(MaintenanceRelease).one().status == "refused", "AMBER without a VERIFIED owner merge never releases"
    assert "owner approval" in db.query(MaintenanceRelease).one().failure_reason


def test_owner_verified_amber_release_proceeds(db, SessionLocal, mset, sset, kernel_factory):
    from services.registry.app.society.seed import seed_society

    seed_society(db)
    raise_incident(db, mset)
    k = kernel_factory(base_script({"DesignRepair": amber_design, "AuthorPatch": amber_patch}))
    run(k, 2, cycles=8)
    provider = pm.FakePromotionProvider()
    for m in (20, 25):
        drive_promotion(SessionLocal, sset, provider)
        run(k, m, cycles=2)
    pr = _case(db).escalation["pr"]["number"]
    provider.human_merge(pr, merged_sha="owner-merge-2")
    drive_promotion(SessionLocal, sset, provider, rounds=2)
    run(k, 50, cycles=2)
    rel = db.query(MaintenanceRelease).one()
    gh, rw, probe = fake_release_env(rel.head_sha, files=(PAGE, MAIN), patch=f"+++ b/{MAIN}\n+def marketplace_badges():\n+    return ['echo']\n")
    gh.owner_merges[pr] = "vansyson1308"
    rc = release_controller(SessionLocal, mset, gh, rw, probe)
    rc._rs = dataclasses.replace(rc.rs, owner_logins=("vansyson1308",))
    for i in range(10):
        rc.run_once(now=at(60 + 2 * i))
    db.expire_all()
    rel = db.query(MaintenanceRelease).one()
    assert rel.status == "succeeded", (rel.status, rel.failure_reason)
    assert rel.verification["checks"]["owner_merged_by"] == "vansyson1308"


def test_red_repair_is_prepared_and_never_released(db, SessionLocal, mset, sset, kernel_factory):
    from services.registry.app.society.seed import seed_society

    seed_society(db)
    raise_incident(db, mset)

    def design(messages, n):
        return {"action": "submit", "result": {"root_cause": "x" * 20, "approach": "touch the wallet module too", "files_allowed": [PAGE, "services/payment/app/wallet.py"], "acceptance_tests": [VERIFY]}}

    def patch(messages, n):
        turns = sum(1 for m in messages if m["role"] == "assistant")
        if turns == 0:
            return {"action": "apply_patch", "args": {"files": [
                {"path": PAGE, "operations": [{"op": "replace_exact", "old": "{'name': 'echo', 'price': 1}", "new": "echo"}]},
                {"path": "services/payment/app/wallet.py", "operations": [{"op": "replace_exact", "old": "BALANCE = 0", "new": "BALANCE = 0  # unchanged semantics"}]},
            ]}}
        return {"action": "submit", "result": {"summary": "s" * 10}}

    k = kernel_factory(base_script({"DesignRepair": design, "AuthorPatch": patch}))
    run(k, 2, cycles=8)
    case = _case(db)
    assert db.query(RepairPlanRevision).one().risk_class == "RED"
    provider = pm.FakePromotionProvider()
    for m in (20, 25):
        drive_promotion(SessionLocal, sset, provider)
        run(k, m, cycles=2)
    case = _case(db)
    assert case.state == "SAFELY_ESCALATED" and case.risk_class == "RED" and provider.merge_count == 0
    provider.human_merge(case.escalation["pr"]["number"], merged_sha="owner-red-merge")
    drive_promotion(SessionLocal, sset, provider, rounds=2)
    run(k, 50, cycles=3)
    case = _case(db)
    assert case.state == "SAFELY_ESCALATED" and case.terminal_reason == "release_requires_owner" and not case.resumable
    assert db.query(MaintenanceRelease).count() == 0, "RED is never handed to the release controller"


def test_constitutional_scope_is_refused_before_any_build(db, mset, kernel_factory):
    raise_incident(db, mset)

    def design(messages, n):
        return {"action": "submit", "result": {"root_cause": "x" * 20, "approach": "rotate the env file", "files_allowed": [PAGE, ".env"], "acceptance_tests": [VERIFY]}}

    k = kernel_factory(base_script({"DesignRepair": design}))
    run(k, 2, cycles=6)
    case = _case(db)
    assert case.state == "POLICY_REFUSED" and case.terminal_reason == "plan_scope_constitutional"
    assert db.query(RepairActivity).filter(RepairActivity.kind == "AuthorPatch").count() == 0
    assert db.query(MaintenanceKnowledge).one().outcome == "POLICY_REFUSED"


def test_rescope_is_a_new_immutable_revision_not_a_new_proposal(db, mset, kernel_factory):
    from services.registry.app.models import CodeCandidate, ImprovementProposal

    raise_incident(db, mset)

    def design(messages, n):
        files = [PAGE] if n == 1 else [PAGE, MAIN]
        return {"action": "submit", "result": {"root_cause": "x" * 20, "approach": "y" * 20, "files_allowed": files, "acceptance_tests": [VERIFY]}}

    def patch(messages, n):
        if n == 1:
            return {"action": "needs_rescope", "result": {"reason": "file_outside_scope", "required_files": [MAIN], "evidence": "the badge helper lives in main.py"}}
        return amber_patch(messages, n)

    k = kernel_factory(base_script({"DesignRepair": design, "AuthorPatch": patch}))
    run(k, 2, cycles=12)
    case = _case(db)
    plans = db.query(RepairPlanRevision).order_by(RepairPlanRevision.revision).all()
    assert [p.revision for p in plans] == [1, 2] and plans[0].files_allowed == [PAGE] and plans[1].files_allowed == [PAGE, MAIN]
    assert plans[1].parent_revision == 1 and plans[1].rescope_reason == "file_outside_scope"
    assert plans[0].risk_class == "MAINTENANCE_GREEN" and plans[1].risk_class == "AMBER"
    assert case.facts["risk_escalations"][0] == {"from": "MAINTENANCE_GREEN", "to": "AMBER", "plan_revision": 2}
    assert case.state == "PROMOTING" and case.rescope_count == 1
    assert db.query(ImprovementProposal).count() == 0 and db.query(CodeCandidate).count() == 1, "no proposal, no Scout, no Governor"


def test_qa_failure_is_repair_input_for_the_next_attempt(db, mset, kernel_factory):
    raise_incident(db, mset)
    seen = {}

    def patch(messages, n):
        turns = sum(1 for m in messages if m["role"] == "assistant")
        if n == 1:
            if turns == 0:
                return {"action": "apply_patch", "args": {"files": [{"path": PAGE, "operations": [{"op": "replace_exact", "old": "{'name': 'echo', 'price': 1}", "new": "{'name': 'echo'}"}]}]}}
            return {"action": "submit", "result": {"summary": "partial fix"}}
        seen["feedback"] = messages[1]["content"]
        return green_repair_script()(messages)

    k = kernel_factory(base_script({"AuthorPatch": patch}))
    run(k, 2, cycles=12)
    attempts = db.query(RepairAttempt).order_by(RepairAttempt.attempt).all()
    assert [a.outcome for a in attempts] == ["rejected", "verified"], [(a.attempt, a.outcome) for a in attempts]
    assert "raw structured value rendered" in seen["feedback"] or "qa_failed" in seen["feedback"]
    assert _case(db).state == "PROMOTING"


def test_owner_kill_switch_stops_autonomy_without_stranding(db, mset, kernel_factory):
    raise_incident(db, mset)
    k = kernel_factory(green_repair_script())
    run(k, 2, cycles=1)
    off = dataclasses.replace(mset, autonomy_enabled=False)
    k2 = kernel_factory(green_repair_script(), settings=off, worker_id="k2")
    st = k2.reconcile(now=at(3))
    case = _case(db)
    assert case.state == "SAFELY_ESCALATED" and case.terminal_reason == "autonomy_disabled" and st.paused == 1
    raise_incident(db, mset, desired_state_ref="login")
    k2.reconcile(now=at(4))
    assert db.query(RepairCase).count() == 1, "no new autonomous repair while the kill switch is on"


def test_healthy_system_means_no_repair_and_zero_model_calls(db, mset, kernel_factory):
    model = ScriptedActivityModel(green_repair_script())
    k = kernel_factory(model=model)
    for i in range(20):
        k.reconcile(now=at(i * 10))
    assert db.query(RepairCase).count() == 0 and model.calls == [] and db.query(RepairActivity).count() == 0


@pytest.mark.parametrize("failure,error_class", [
    ("not json at all", "invalid_json"),
    (ModelReply('{"action": "submit", "res', 10, 10, finish_reason="length"), "partial"),
    (asyncio.TimeoutError(), "timeout"),
    (ActivityError("rate_limit", "429"), "rate_limit"),
    ({"action": "submit", "result": {"root_cause": "short"}}, "invalid_output"),
])
def test_model_failures_retry_by_policy_then_escalate_never_strand(db, mset, kernel_factory, failure, error_class):
    from services.registry.app.maintenance.reconciler import stranded_count

    raise_incident(db, mset)

    def diag(messages, n):
        return failure

    k = kernel_factory(base_script({"DiagnoseIncident": diag}))
    for i in range(12):
        k.reconcile(now=at(2 + i * 2))
        assert stranded_count(db, now=at(2 + i * 2)) == 0
    case = _case(db)
    rows = db.query(RepairActivity).filter(RepairActivity.kind == "DiagnoseIncident").all()
    assert len(rows) == mset.activity_max_tries and all(r.status == "failed" and r.error_class == error_class for r in rows), [(r.status, r.error_class) for r in rows]
    assert case.state == "SAFELY_ESCALATED" and case.terminal_reason == "activity_exhausted:DiagnoseIncident"


def test_a_transient_model_failure_recovers_within_the_same_case(db, mset, kernel_factory):
    raise_incident(db, mset)

    def diag(messages, n):
        if n == 1:
            return ActivityError("rate_limit", "429")
        return green_repair_script()(messages)

    k = kernel_factory(base_script({"DiagnoseIncident": diag}))
    for i in range(10):
        k.reconcile(now=at(2 + i * 2))
    assert _case(db).state == "PROMOTING"
    kinds = [(a.kind, a.status) for a in db.query(RepairActivity).filter(RepairActivity.kind == "DiagnoseIncident").order_by(RepairActivity.started_at).all()]
    assert kinds[0] == ("DiagnoseIncident", "failed") and ("DiagnoseIncident", "succeeded") in kinds


def test_github_release_failures_are_bounded_and_fail_closed(db, SessionLocal, mset, sset, kernel_factory):
    from services.registry.app.maintenance.release_providers import ProviderTransient

    from .test_kernel_e2e import reach_releasing

    reach_releasing(db, SessionLocal, mset, sset, kernel_factory)
    rel = db.query(MaintenanceRelease).one()
    gh, rw, probe = fake_release_env(rel.head_sha)
    gh.faults = [ProviderTransient("github timeout")] * 2
    gh.conflict = True
    rc = release_controller(SessionLocal, mset, gh, rw, probe)
    for i in range(10):
        rc.run_once(now=at(40 + i * 5))
    db.expire_all()
    rel = db.query(MaintenanceRelease).one()
    assert rel.status == "refused" and "merge conflict" in rel.failure_reason
    assert gh.merges == 0 and rw.deploy_calls == 0 and len(gh.prs) == 1, "no force push, no duplicate PR storm"
    run(kernel_factory(green_repair_script(), worker_id="k9"), 90, cycles=2)
    assert _case(db).state == "SAFELY_ESCALATED" and _case(db).terminal_reason.startswith("release_refused")


@pytest.mark.parametrize("tamper,needle", [
    ("unrelated", "unrelated unreleased changes"),
    ("red_diff", "trusted classification is RED"),
    ("ci_failed", "required checks failed"),
    ("tampered", "digest mismatch"),
    ("production_moved", "production moved"),
    ("schema", "Railway schema lacks"),
])
def test_release_controller_recomputes_and_refuses(db, SessionLocal, mset, sset, kernel_factory, tamper, needle):
    from sqlalchemy.orm.attributes import flag_modified

    from .test_kernel_e2e import reach_releasing

    reach_releasing(db, SessionLocal, mset, sset, kernel_factory)
    rel = db.query(MaintenanceRelease).one()
    gh, rw, probe = fake_release_env(rel.head_sha)
    if tamper == "unrelated":
        gh.files_between[rel.head_sha] = [PAGE, "services/registry/app/api/routes/agents.py"]
    elif tamper == "red_diff":
        rel.attestation = {**rel.attestation, "changed_files": ["services/payment/app/wallet.py"]}
        from services.registry.app.maintenance import attestation as att

        rel.attestation_digest, rel.attestation_signature = att.sign(rel.attestation)
        gh.files_between[rel.head_sha] = ["services/payment/app/wallet.py"]
        db.commit()
    elif tamper == "ci_failed":
        gh.check_state[rel.head_sha] = "failed"
    elif tamper == "tampered":
        rel.attestation["risk_decision"] = {"class": "MAINTENANCE_GREEN", "reasons": ["trust me"]}
        flag_modified(rel, "attestation")
        db.commit()
    elif tamper == "schema":
        rw.schema_ok = False
    rc = release_controller(SessionLocal, mset, gh, rw, probe)
    if tamper == "production_moved":
        rc.run_once(now=at(40))
        gh.production = "g1"
    for i in range(6):
        rc.run_once(now=at(41 + i * 2))
    db.expire_all()
    rel = db.query(MaintenanceRelease).one()
    assert rel.status == "refused" and needle in (rel.failure_reason or ""), (rel.status, rel.failure_reason)
    assert gh.merges == 0 and rw.deploy_calls == 0


def test_rollback_falls_back_to_the_known_good_sha_and_a_failed_rollback_is_p0(db, SessionLocal, mset, sset, kernel_factory):
    from services.registry.app.maintenance.orm import MaintenanceReleaseFreeze
    from services.registry.app.maintenance.release_providers import FakeProbe, ProviderRefused

    from .test_kernel_e2e import reach_releasing

    k, _ = reach_releasing(db, SessionLocal, mset, sset, kernel_factory)
    rel = db.query(MaintenanceRelease).one()
    gh, rw, _ = fake_release_env(rel.head_sha)
    rw.rollback_error = ProviderRefused("image expired")
    rw.rollback_outcome = "SUCCESS"
    probe = FakeProbe([{"ui_root": True}, {"ui_root": False}])  # never recovers
    rc = release_controller(SessionLocal, mset, gh, rw, probe)
    for i in range(30):
        rc.run_once(now=at(40 + i * 10))
    db.expire_all()
    rel = db.query(MaintenanceRelease).one()
    assert rel.rollback["services"]["dashboard"]["method"] == "redeploy_known_good_sha" and rel.rollback["services"]["dashboard"]["to"] == "g0"
    assert rel.status == "rollback_failed" and "not restored" in rel.failure_reason
    fr = db.query(MaintenanceReleaseFreeze).filter(MaintenanceReleaseFreeze.reason_code == "rollback_failed_p0").one()
    assert fr.owner_only and fr.lifted_at is None
    run(k, 400, cycles=2)
    case = _case(db)
    assert case.state == "SAFELY_ESCALATED" and case.terminal_reason == "rollback_failed" and not case.resumable
    assert rw.rollback_calls == 1 and rw.deploy_calls == 2, "one rollback attempt, one known-good redeploy: no retry storm"
