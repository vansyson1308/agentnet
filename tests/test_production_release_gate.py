"""The trusted production release gate (Phase 7 §17-21, §43).

Every test here drives the gate with injected facts, so none of it touches the
network or a live Railway. The point is not that the gate *can* pass -- it is
that it REFUSES the specific things that would let unreviewed or untested code
reach production, and that the Society cannot reach the gate at all.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from deploy.production import release as rel

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ── a tiny real git repository, so tree comparison is real ──────────────
def _run(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    """A real repo with a main line and two service subtrees."""
    root = tmp_path / "repo"
    root.mkdir()
    _run("git", "init", "-q", "-b", "main", cwd=root)
    _run("git", "config", "user.email", "t@t.test", cwd=root)
    _run("git", "config", "user.name", "t", cwd=root)
    for svc in ("registry", "payment", "worker", "dashboard"):
        d = root / "services" / svc
        d.mkdir(parents=True)
        (d / "app.py").write_text(f"# {svc} v1\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "x.md").write_text("v1\n", encoding="utf-8")
    _run("git", "add", "-A", cwd=root)
    _run("git", "commit", "-qm", "base", cwd=root)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    # origin/main must resolve for the reachability check
    _run("git", "update-ref", "refs/remotes/origin/main", base, cwd=root)
    return root, base


def _commit(root, message):
    _run("git", "add", "-A", cwd=root)
    _run("git", "commit", "-qm", message, cwd=root)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    _run("git", "update-ref", "refs/remotes/origin/main", sha, cwd=root)
    return sha


def _gate(root, target, *, ci="success", staging=None, current=None, allow_sensitive=False, freeze=()):
    git = rel.GitFacts(cwd=str(root))
    staging = staging or {}
    return rel.preflight(
        target=target,
        git=git,
        main_ci=lambda _sha: ci,
        staging_sha=lambda svc: staging.get(svc),
        current_release=current,
        allow_sensitive=allow_sensitive,
        merge_freeze_reasons=freeze,
    )


def _all_staged(sha):
    return {s: sha for s in rel.SERVICE_PATHS}


# ── target validation ───────────────────────────────────────────────────
def test_an_arbitrary_sha_is_rejected(repo):
    root, base = repo
    verdict = _gate(root, "0" * 40, staging=_all_staged(base))
    assert not verdict.ok
    assert "target_exists" in verdict.blocking


def test_latest_is_not_a_target(repo):
    root, base = repo
    verdict = _gate(root, "latest", staging=_all_staged(base))
    assert not verdict.ok
    assert "target_shape" in verdict.blocking


def test_a_commit_not_reachable_from_main_is_rejected(repo):
    """A real commit is not enough -- it has to be on the line staging tests."""
    root, base = repo
    _run("git", "checkout", "-q", "-b", "side", cwd=root)
    (root / "services" / "registry" / "app.py").write_text("# side\n", encoding="utf-8")
    _run("git", "add", "-A", cwd=root)
    _run("git", "commit", "-qm", "side work", cwd=root)
    side = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    verdict = _gate(root, side, staging=_all_staged(side))
    assert not verdict.ok
    assert "target_on_main" in verdict.blocking


def test_a_ci_red_target_is_rejected(repo):
    root, base = repo
    verdict = _gate(root, base, ci="failure", staging=_all_staged(base))
    assert not verdict.ok
    assert "target_ci_green" in verdict.blocking


def test_a_pending_target_is_rejected(repo):
    root, base = repo
    verdict = _gate(root, base, ci="pending", staging=_all_staged(base))
    assert not verdict.ok and "target_ci_green" in verdict.blocking


def test_an_active_autonomous_merge_freeze_blocks_release(repo):
    root, base = repo
    verdict = _gate(root, base, staging=_all_staged(base), freeze=("daily_merge_cap_reached(1)",))
    assert not verdict.ok
    assert "no_autonomous_merge_freeze" in verdict.blocking


# ── staging evidence ────────────────────────────────────────────────────
def test_runtime_subtree_changed_but_never_staged_is_blocked(repo):
    """The case that matters: code that production would run and staging never did."""
    root, base = repo
    (root / "services" / "registry" / "app.py").write_text("# registry v2\n", encoding="utf-8")
    target = _commit(root, "registry change")
    verdict = _gate(root, target, staging=_all_staged(base))  # staging still on base
    assert not verdict.ok
    assert "staging_evidence:registry" in verdict.blocking
    detail = [c for c in verdict.checks if c["check"] == "staging_evidence:registry"][0]["detail"]
    assert "CHANGED" in detail and "never deployed" in detail


def test_unchanged_subtree_inherits_the_prior_staging_deployment(repo):
    """A docs-only merge legitimately SKIPS a Railway deploy (watch paths).
    Requiring deployed-SHA equality would make such a target unreleasable."""
    root, base = repo
    (root / "docs" / "x.md").write_text("v2 docs only\n", encoding="utf-8")
    target = _commit(root, "docs only")
    verdict = _gate(root, target, staging=_all_staged(base))
    assert verdict.ok, verdict.blocking
    detail = [c for c in verdict.checks if c["check"] == "staging_evidence:registry"][0]["detail"]
    assert "unchanged since staging-deployed" in detail


def test_staging_deployed_the_target_itself_is_evidence(repo):
    root, base = repo
    (root / "services" / "worker" / "app.py").write_text("# worker v2\n", encoding="utf-8")
    target = _commit(root, "worker change")
    verdict = _gate(root, target, staging=_all_staged(target))
    assert verdict.ok, verdict.blocking


def test_a_service_with_no_staging_deployment_is_blocked(repo):
    root, base = repo
    staging = _all_staged(base)
    del staging["payment"]
    verdict = _gate(root, base, staging=staging)
    assert not verdict.ok
    assert "staging_evidence:payment" in verdict.blocking


# ── sensitive diff classification ───────────────────────────────────────
@pytest.mark.parametrize(
    "path, category",
    [
        ("services/registry/migrations/versions/0011_x.py", "migrations"),
        ("init-db/17-new.sql", "db_bootstrap"),
        ("services/registry/app/api/routes/auth.py", "auth"),
        ("services/payment/app/main.py", "payment_economics"),
        ("services/registry/app/society/risk.py", "society_policy"),
        ("services/registry/app/society/github_credentials.py", "credential_boundary"),
        ("deploy/production/release.py", "release_machinery"),
        (".github/workflows/ci.yml", "ci"),
        ("services/registry/Dockerfile", "deployment_foundation"),
        (".railway/production.ts", "release_machinery"),
    ],
)
def test_sensitive_paths_are_classified(path, category):
    assert rel.classify_sensitive([path]).get(category) == [path]


def test_ordinary_application_and_docs_paths_are_not_sensitive():
    ordinary = ["docs/README.md", "services/registry/app/api/routes/agents.py",
                "services/dashboard/templates/index.html", "tests/test_x.py"]
    assert rel.classify_sensitive(ordinary) == {}


def test_a_migration_in_the_release_diff_fails_closed(repo):
    """Code rollback is not schema rollback (ADR-0008 D7)."""
    root, base = repo
    d = root / "services" / "registry" / "migrations" / "versions"
    d.mkdir(parents=True)
    (d / "0011_add_column.py").write_text("# migration\n", encoding="utf-8")
    target = _commit(root, "add migration")
    verdict = _gate(root, target, staging=_all_staged(target), current=base)
    assert not verdict.ok
    assert "no_sensitive_changes" in verdict.blocking
    assert "migrations" in verdict.sensitive


def test_a_sensitive_release_can_be_acknowledged_explicitly(repo):
    root, base = repo
    d = root / "services" / "registry" / "migrations" / "versions"
    d.mkdir(parents=True)
    (d / "0011_add_column.py").write_text("# migration\n", encoding="utf-8")
    target = _commit(root, "add migration")
    verdict = _gate(root, target, staging=_all_staged(target), current=base, allow_sensitive=True)
    assert verdict.ok, verdict.blocking
    assert "migrations" in verdict.sensitive, "acknowledged, but still recorded"


def test_an_ordinary_release_passes_the_sensitive_gate(repo):
    root, base = repo
    (root / "services" / "dashboard" / "app.py").write_text("# dashboard v2\n", encoding="utf-8")
    target = _commit(root, "ui tweak")
    verdict = _gate(root, target, staging=_all_staged(target), current=base)
    assert verdict.ok, verdict.blocking
    assert verdict.sensitive == {}


# ── tree equality ───────────────────────────────────────────────────────
def test_release_tree_must_equal_the_approved_target_tree(repo):
    root, base = repo
    git = rel.GitFacts(cwd=str(root))
    (root / "docs" / "x.md").write_text("drifted\n", encoding="utf-8")
    drifted = _commit(root, "drift")
    rel.verify_tree_equality(git, base, base)          # identical: fine
    with pytest.raises(rel.ReleaseBlocked):
        rel.verify_tree_equality(git, base, drifted)   # different source: refused


def test_a_wrapper_commit_with_the_same_tree_is_accepted(repo):
    """The release commit SHA may differ; the deployed source must not."""
    root, base = repo
    git = rel.GitFacts(cwd=str(root))
    _run("git", "commit", "-q", "--allow-empty", "-m", "release wrapper", cwd=root)
    wrapper = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    assert wrapper != base
    rel.verify_tree_equality(git, base, wrapper)


# ── the constitutional boundary ─────────────────────────────────────────
def test_the_society_runtime_cannot_reach_the_release_gate():
    """No Society module may import the production release machinery, and no
    intent may name it. If this ever fails, the Society has a path to
    production authority."""
    society = REPO_ROOT / "services" / "registry" / "app" / "society"
    offenders = []
    for path in society.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "deploy.production" in text or "deploy/production" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"Society modules must not reference the release gate: {offenders}"


def test_production_deploy_stays_hard_off_in_society_settings():
    """Phase 7 must not be read as permission to remove this invariant."""
    config = (REPO_ROOT / "services" / "registry" / "app" / "society" / "config.py").read_text(encoding="utf-8")
    assert "production_deploy_enabled: bool = False" in config


def test_production_iac_refuses_any_other_environment():
    ts = (REPO_ROOT / ".railway" / "production.ts").read_text(encoding="utf-8")
    assert 'ctx.environment !== "production"' in ts
    assert "throw new Error" in ts


def _ts_code_only(text: str) -> str:
    """Strip /* */ and // comments.

    The file's header explains at length WHY there is no society-worker and no
    model credential; a raw substring scan would flag that prose and force the
    documentation to be deleted to make the test pass. The claim under test is
    about the declared topology, so read the code.
    """
    import re

    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def test_production_iac_declares_no_society_and_no_model_credential():
    ts = _ts_code_only((REPO_ROOT / ".railway" / "production.ts").read_text(encoding="utf-8"))
    for forbidden in (
        "society-worker", "SOCIETY_MODEL_API_KEY", "SOCIETY_GITHUB_APP_PRIVATE_KEY_PEM",
        "SOCIETY_GITHUB_APP_PRIVATE_KEY_FILE", "SOCIETY_GITHUB_TOKEN", "LLM_API_KEY",
    ):
        assert forbidden not in ts, f"{forbidden} must never appear in the production topology"
    # ...and the Society switches it does declare are all off.
    for off in ("SOCIETY_RUNTIME_ENABLED", "SOCIETY_AUTONOMOUS_CODE_ENABLED", "SOCIETY_AUTO_MERGE_ENABLED"):
        assert f'{off}: "false"' in ts


def test_production_iac_deploys_the_production_branch_not_main():
    """If production followed main, staging evaluation would be decorative and
    the Society would hold production authority through a branch it can write."""
    ts = (REPO_ROOT / ".railway" / "production.ts").read_text(encoding="utf-8")
    assert 'const BRANCH = "production"' in ts


# ── the branch rulesets (Phase 7 §16) ────────────────────────────────────────
#
# The rulesets are JSON an owner applies by hand, so nothing in CI would notice
# them drifting out of sync with CI itself. That matters in both directions: a
# required check whose job no longer exists is permanently pending, which LOCKS
# the branch, and a job that exists but is not required is a gate that silently
# does nothing. These tests make the JSON answerable to the workflow.

def _ci_job_names() -> set:
    import re

    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    return set(re.findall(r"^    name: (.+)$", text, flags=re.M))


def _ruleset(name: str) -> dict:
    import json

    return json.loads((REPO_ROOT / "deploy" / "github" / name).read_text(encoding="utf-8"))


def _required_contexts(ruleset: dict) -> set:
    for rule in ruleset["rules"]:
        if rule["type"] == "required_status_checks":
            return {c["context"] for c in rule["parameters"]["required_status_checks"]}
    raise AssertionError("ruleset declares no required status checks")


@pytest.mark.parametrize("name", ["main-ruleset.json", "production-ruleset.json"])
def test_ruleset_required_checks_are_exactly_the_ci_jobs(name):
    assert _required_contexts(_ruleset(name)) == _ci_job_names()


@pytest.mark.parametrize("name", ["main-ruleset.json", "production-ruleset.json"])
def test_ruleset_has_no_bypass_actors(name):
    """No bypass actor at all -- in particular not the Society GitHub App,
    which would otherwise turn every protection here into a suggestion."""
    ruleset = _ruleset(name)
    assert ruleset["bypass_actors"] == []
    assert ruleset["enforcement"] == "active"
    types = {rule["type"] for rule in ruleset["rules"]}
    assert {"deletion", "non_fast_forward", "pull_request", "required_status_checks"} <= types


def test_production_ruleset_targets_the_production_branch():
    ruleset = _ruleset("production-ruleset.json")
    assert ruleset["conditions"]["ref_name"]["include"] == ["refs/heads/production"]
    assert ruleset["conditions"]["ref_name"]["exclude"] == []


def test_production_ruleset_forbids_rebase_merges():
    """A rebase rewrites the tree lineage the release gate verifies, so a
    released commit could no longer be shown to be the approved target."""
    for rule in _ruleset("production-ruleset.json")["rules"]:
        if rule["type"] == "pull_request":
            assert "rebase" not in rule["parameters"]["allowed_merge_methods"]
            return
    raise AssertionError("production ruleset declares no pull_request rule")


def test_ci_runs_on_the_production_branch():
    """Railway's Wait for CI needs an `on: push` directive for the branch it
    gates (ADR-0008 D3); required status checks need the `pull_request` one.
    Without both, production's gate is vacuous rather than strict."""
    import re

    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    branch_lists = re.findall(r"^    branches: \[(.+)\]$", text, flags=re.M)
    assert len(branch_lists) == 2, "expected a push and a pull_request branch filter"
    for entry in branch_lists:
        names = {b.strip() for b in entry.split(",")}
        assert "production" in names and "main" in names


def test_production_declares_email_delivery_and_never_logs_the_link():
    """Production must not fall back to the log provider: the verification link
    is a live credential. `disabled` is the honest posture until an owner wires
    a real sender -- registration then refuses with 503 rather than creating
    accounts nobody can activate."""
    ts = _ts_code_only((REPO_ROOT / ".railway" / "production.ts").read_text(encoding="utf-8"))
    assert 'EMAIL_DELIVERY_PROVIDER: "disabled"' in ts
    assert 'EMAIL_DELIVERY_PROVIDER: "log"' not in ts
