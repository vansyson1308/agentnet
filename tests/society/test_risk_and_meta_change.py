"""Trusted risk classifier + meta-change safety (a candidate cannot reclassify itself)."""

from __future__ import annotations

import uuid

import pytest

from services.registry.app.models import CodeCandidate, RiskTier
from services.registry.app.society import risk
from services.registry.app.society.engineering import workspace as ws_mod
from services.registry.app.society.engineering.qa import evaluate_candidate
from services.registry.app.society.intents import FileEdit


@pytest.mark.parametrize(
    "path,tier",
    [
        ("docs/SELF_DEVELOPMENT.md", "green"),
        ("services/dashboard/app/templates/x.html", "green"),
        ("tests/society/acceptance/test_candidate_docs.py", "green"),
        ("services/registry/app/api/routes/stats.py", "amber"),
        ("services/worker/app/worker.py", "amber"),
        ("sdk/python/agentnet/client.py", "amber"),
        ("tests/test_sdk.py", "amber"),
        ("services/registry/app/society/policy.py", "red"),
        ("services/registry/app/society/risk.py", "red"),
        ("services/registry/app/society/fitness.py", "red"),
        ("services/registry/app/auth.py", "red"),
        ("services/registry/app/authz.py", "red"),
        ("services/payment/app/api/routes/wallets.py", "red"),
        ("services/registry/app/task_service.py", "red"),
        ("services/registry/migrations/versions/0011_x.py", "red"),
        ("services/registry/init-db/18-x.sql", "red"),
        ("deploy/society-migration-check.sh", "red"),
        (".github/workflows/ci.yml", "red"),
        ("docker-compose.staging.yml", "red"),
        ("services/registry/Dockerfile", "red"),
        ("requirements-dev.txt", "red"),
        ("pytest.ini", "red"),
        ("scripts/ci/check_skips.py", "red"),
        (".env", "never"),
        (".env.example", "never"),
        ("secrets/prod.json", "never"),
        ("deploy/keys/server.pem", "never"),
        (".git/config", "never"),
    ],
)
def test_tier_for_path(path, tier):
    assert risk.tier_for_path(path).value == tier


def test_max_tier_and_code_floor():
    assert risk.max_tier([RiskTier.GREEN, RiskTier.AMBER, RiskTier.GREEN]) == RiskTier.AMBER
    assert risk.assess(["docs/a.md"], "", spec_kind="code").tier == RiskTier.AMBER
    assert risk.assess(["docs/a.md"], "").tier == RiskTier.GREEN
    assert risk.assess([]).tier == RiskTier.GREEN


@pytest.mark.parametrize(
    "diff,label",
    [
        ("--- a/tests/test_a.py\n+++ b/tests/test_a.py\n-def test_x():\n-    assert 1\n", "test removed"),
        ("--- a/tests/test_a.py\n+++ b/tests/test_a.py\n+@pytest.mark.skip\n", "test skipped"),
        ("--- a/tests/test_a.py\n+++ b/tests/test_a.py\n+    pytest.skip('later')\n", "test skipped"),
        ("--- a/pytest.ini\n+++ b/pytest.ini\n-    error::pytest.PytestUnknownMarkWarning\n", "warning gate removed"),
        ("--- a/x.py\n+++ b/x.py\n+    subprocess.run(cmd, shell=True)\n", "unrestricted shell added"),
        ("--- a/x.py\n+++ b/x.py\n+    os.system('rm -rf /')\n", "unrestricted shell added"),
        ("--- a/x.py\n+++ b/x.py\n+    token = os.environ['SOCIETY_GITHUB_TOKEN']\n", "credential reference added"),
        ("--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n+    continue-on-error: true\n", "CI job made non-blocking"),
        ("--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n+    if: false\n", "CI job disabled"),
        ("--- a/tests/test_a.py\n+++ /dev/null\n-def test_x():\n", "file deleted"),
    ],
)
def test_diff_never_findings(diff, label):
    findings = risk.diff_never_findings(diff, ["tests/test_a.py"])
    assert any(label in f for f in findings), findings
    assert risk.assess(["tests/test_a.py"], diff).tier == RiskTier.NEVER


def test_assertion_removal_outside_tests_is_not_never():
    diff = "--- a/services/x.py\n+++ b/services/x.py\n-    assert value\n+    if not value: raise ValueError\n"
    assert not any("assertion removed" in f for f in risk.diff_never_findings(diff, ["services/x.py"]))


# ── META-CHANGE ATTACK: candidate rewrites risk.py to call itself GREEN ────


def _candidate_editing(settings, rel_path: str, content: str, extra=None):
    cid = uuid.uuid4()
    ws = ws_mod.ensure_workspace(settings, cid)
    edits = [FileEdit(path=rel_path, content=content)] + [FileEdit(path=p, content=c) for p, c in (extra or [])]
    ws_mod.apply_edits(ws, edits, allowed=[rel_path] + [p for p, _ in (extra or [])])
    ws_mod.commit_all(ws, "attack")
    return cid, ws


def test_candidate_may_edit_policy_but_is_classified_by_the_trusted_base(society_settings, temp_repo):
    """The candidate edits BOTH services/registry/app/society/policy.py and
    risk.py (declaring the whole society tree GREEN). The running classifier
    still says RED — the worktree copy is never imported."""
    forged_risk = 'GREEN_PATTERNS = ("**",)\nRED_PATTERNS = ()\ndef tier_for_path(p):\n    return "green"\n'
    cid, ws = _candidate_editing(
        society_settings,
        "services/registry/app/society/policy.py",
        "RISK_BY_TYPE = {}\n",
        extra=[("services/registry/app/society/risk.py", forged_risk)],
    )
    changed = ws_mod.changed_files(ws)
    assert set(changed) == {"services/registry/app/society/policy.py", "services/registry/app/society/risk.py"}
    assessment = risk.assess(changed, ws_mod.diff_text(ws), spec_kind="code")
    assert assessment.tier == RiskTier.RED
    assert risk.tier_for_path("services/registry/app/society/policy.py") == RiskTier.RED  # base module unchanged
    # QA also reports the trusted tier and flags nothing as GREEN
    spec = {"files_allowed": changed, "acceptance_tests": ["tests/society/acceptance/test_candidate_docs.py"], "kind": "code"}
    report = evaluate_candidate(society_settings, ws, spec, changed)
    assert report.__dict__["risk_tier"] == "red"


def test_candidate_cannot_reclassify_through_the_database(db, society_settings, temp_repo):
    """Even if a candidate row carries risk_tier='green', promotion validation
    recomputes it from the diff with the base classifier."""
    from services.registry.app.society import promotion as promo_mod

    cid, ws = _candidate_editing(society_settings, "services/registry/app/society/fitness.py", "TRUSTED_CRITERIA = {'hard_gates': []}\n")
    cand = CodeCandidate(
        id=cid,
        correlation_id=uuid.uuid4(),
        title="reward hack",
        spec={"files_allowed": ["services/registry/app/society/fitness.py"], "acceptance_tests": ["tests/society/acceptance/test_candidate_docs.py"], "kind": "code"},
        status="ready",
        branch_name=ws.branch,
        base_sha=ws.base_sha,
        head_sha=ws_mod.head_sha(ws),
        changed_files=ws_mod.changed_files(ws),
        risk_tier="green",  # forged
        qa_report={"verdict": "pass", "head_sha": ws_mod.head_sha(ws)},
        security_report={"verdict": "pass", "head_sha": ws_mod.head_sha(ws)},
        requires_security_review=False,
        diff_lines=1,
    )
    db.add(cand)
    db.commit()
    from services.registry.app.models import CodePromotion, PromotionStatus

    promo = CodePromotion(id=uuid.uuid4(), candidate_id=cand.id, correlation_id=cand.correlation_id, risk_tier="green", provider="fake", status=PromotionStatus.VALIDATING, base_sha=cand.base_sha, candidate_sha=cand.head_sha)
    db.add(promo)
    db.commit()
    with pytest.raises(ValueError, match="INDEPENDENT Security"):
        promo_mod.validate(db, society_settings, promo, cand)
    db.flush()
    assert cand.risk_tier == "red" and promo.risk_tier == "red"


def test_never_writable_paths_are_refused_by_the_workspace(society_settings, temp_repo):
    ws = ws_mod.ensure_workspace(society_settings, uuid.uuid4())
    for bad in (".env", "config/secrets/x.json", "server.pem", "id_rsa"):
        with pytest.raises(ws_mod.WorkspaceError):
            ws_mod.apply_edits(ws, [FileEdit(path=bad, content="x")], allowed=[bad])
    # RED surfaces ARE writable now (proposable), so a Security + human gate applies later
    ws_mod.apply_edits(ws, [FileEdit(path="services/registry/app/society/policy.py", content="# proposal\n")], allowed=["services/registry/app/society/policy.py"])
    assert (ws.path / "services/registry/app/society/policy.py").exists()
