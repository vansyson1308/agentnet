"""The trusted production release gate (Phase 7 §17-21, §43).

Every test here drives the gate with injected facts, so none of it touches the
network or a live Railway. The point is not that the gate *can* pass -- it is
that it REFUSES the specific things that would let unreviewed or untested code
reach production, and that the Society cannot reach the gate at all.
"""

from __future__ import annotations

import pathlib
import re
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

    Quote-aware: a `//` inside a string is a URL (`"https://..."`), not a
    comment, and stripping it would delete exactly the values under test.
    """
    out, i, quote = [], 0, ""
    while i < len(text):
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'`":
            quote = ch
            out.append(ch)
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


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


def test_production_iac_declares_live_a2a_without_a_society_client():
    """A2A is live in production (docs/A2A_LIVE_PROOF.md). The vault key is a
    shared reference, never a literal; the Society's A2A client and the
    company cycle stay off because there is no production Society."""
    ts = _ts_code_only((REPO_ROOT / ".railway" / "production.ts").read_text(encoding="utf-8"))
    assert 'A2A_SERVER_ENABLED: "true"' in ts and 'A2A_FEDERATION_ENABLED: "true"' in ts
    assert "A2A_CREDENTIAL_KEY: ctx.shared.A2A_CREDENTIAL_KEY" in ts
    assert 'A2A_SOCIETY_CLIENT_ENABLED: "false"' in ts and 'SOCIETY_COMPANY_CYCLE_ENABLED: "false"' in ts
    assert "A2A_PUBLIC_BASE_URL: PUBLIC_API_ORIGIN" in ts
    assert 'SOCIETY_OPERATOR_BOOTSTRAP_EMAILS: ""' in ts


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
    is a live credential. Delivery is live SMTP (Resend) since the email
    cutover, and the IaC must say so -- an apply of a file still declaring
    `disabled` would silently turn public signup back off."""
    ts = _ts_code_only((REPO_ROOT / ".railway" / "production.ts").read_text(encoding="utf-8"))
    assert 'EMAIL_DELIVERY_PROVIDER: "smtp"' in ts
    assert 'EMAIL_DELIVERY_PROVIDER: "log"' not in ts
    assert 'EMAIL_DELIVERY_PROVIDER: "disabled"' not in ts


# ── IaC parity with the live environment (ADR-0008 D13) ─────────────────────
#
# In a one-file Railway IaC project, omitting a resource or a variable DELETES
# it on apply. These tests pin the properties whose loss would be destructive
# or would silently change what production is.

def _prod_ts() -> str:
    return _ts_code_only((REPO_ROOT / ".railway" / "production.ts").read_text(encoding="utf-8"))


def test_production_iac_names_the_live_production_services_not_stagings():
    """Railway services are project-wide: the unprefixed names are STAGING's.
    Declaring them here would pull staging services into production, and
    leaving the prod-* services undeclared would delete them."""
    import re

    ts = _prod_ts()
    declared = set(re.findall(r'\bservice\("([^"]+)"', ts))
    assert declared == {
        "prod-postgres", "prod-redis", "prod-registry", "prod-payment", "prod-worker", "prod-dashboard",
    }
    # the database-product helpers carry their own image/mount defaults and a
    # different resource address than the live image services
    assert "postgres(" not in ts and "redis(" not in ts
    for staging_ref in ("${{registry.", "${{dashboard.", "${{payment.", "${{worker."):
        assert staging_ref not in ts, staging_ref


def test_production_iac_keeps_the_data_volumes_mounted_where_the_data_is():
    ts = _prod_ts()
    assert 'volume("prod-postgres-volume", { region: REGION, sizeMB: 5000 })' in ts
    assert 'volume("prod-redis-volume", { region: REGION, sizeMB: 5000 })' in ts
    assert '"/var/lib/postgresql/data": postgresVolume' in ts
    assert '"/data": redisVolume' in ts
    assert 'image("ghcr.io/railwayapp-templates/postgres-ssl:18")' in ts
    assert 'image("redis:8.2")' in ts
    # the fail-closed Redis start command (ADR-0008 D11) is part of the topology
    assert "refusing to start an unauthenticated Redis" in ts
    assert '--requirepass \\"$REDIS_PASSWORD\\"' in ts


def test_production_iac_pins_the_canonical_public_api_origin():
    """Verification links are built from PUBLIC_BASE_URL. It must be the
    canonical API origin, never `${{RAILWAY_PUBLIC_DOMAIN}}`: production is
    dark, so that renders `https://`, which the registry refuses at boot."""
    ts = _prod_ts()
    assert 'const PUBLIC_API_ORIGIN = "https://api.agentnet.io.vn";' in ts
    assert "PUBLIC_BASE_URL: PUBLIC_API_ORIGIN," in ts
    assert "RAILWAY_PUBLIC_DOMAIN" not in ts


def test_production_iac_pins_the_proven_smtp_configuration():
    """Railway egress blocks 465 and 587; 2465 is the port measured to work."""
    ts = _prod_ts()
    for line in (
        'SMTP_HOST: "smtp.resend.com"',
        'SMTP_PORT: "2465"',
        'SMTP_USERNAME: "resend"',
        'SMTP_FROM: "AgentNet <noreply@mail.agentnet.io.vn>"',
        'SMTP_TLS: "true"',
        'SMTP_STARTTLS: "false"',
    ):
        assert line in ts, line


def test_production_iac_never_holds_a_secret_value():
    """Owner-managed and generated secrets are preserve(); cross-service ones
    are references. A literal would put a credential in git -- and an omitted
    one would be DELETED on apply, which is why each is declared at all."""
    import re

    ts = _prod_ts()
    assert "SMTP_PASSWORD: preserve()," in ts
    assert "POSTGRES_PASSWORD: preserve()," in ts
    assert "REDIS_PASSWORD: preserve()," in ts
    # EVERY occurrence, whatever its quoting: one correct declaration must not
    # vouch for a literal on another service.
    allowed = ("preserve()", "ctx.shared.", "db.env.", "cache.env.", '"${{')
    for name in ("SMTP_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "JWT_SECRET_KEY",
                 "FLASK_SECRET_KEY", "INTERNAL_WORKER_TOKEN", "PGPASSWORD", "REDISPASSWORD"):
        values = re.findall(rf"\b{name}\s*:\s*(\S+)", ts)
        assert values, f"{name} is not declared at all (an omitted variable is DELETED on apply)"
        for value in values:
            assert value.startswith(allowed), f"{name} must be a reference or preserve(), not {value[:6]}..."
    for secret in ("JWT_SECRET_KEY", "FLASK_SECRET_KEY", "INTERNAL_WORKER_TOKEN"):
        assert f"{secret}: ctx.shared.{secret}" in ts


def test_production_iac_waits_for_ci_on_every_app_service():
    """Wait for CI is expressible (`checkSuites`) and every app service shares
    the one helper that sets it, so no service can drop it alone."""
    ts = _prod_ts()
    assert "checkSuites: true" in ts
    assert "branch: BRANCH" in ts
    for svc in ("registry", "payment", "worker", "dashboard"):
        assert f'...app("services/{svc}")' in ts


def _domains_of(block):
    """(domain, port) pairs declared inside one service block, in order."""
    start = block.find("domains: [")
    if start == -1:
        return []
    end = block.index("]", start)
    return re.findall(r'\{ domain: "([^"]+)", port: (\d+) \}', block[start:end])


def test_production_iac_exposes_exactly_the_three_public_domains():
    """Exactly api -> registry:8000, and the apex (canonical UI) plus the
    dashboard compatibility host -> dashboard:8080. All three exist on Railway;
    an undeclared custom domain is DELETED on apply, so dropping one here would
    take it down. Any other exposure (a TCP proxy, a Railway service domain, a
    domain on payment/worker/Postgres/Redis, www, payment, staging) is a new
    public surface."""
    ts = _prod_ts()
    registry = ts[ts.index('service("prod-registry"'):ts.index('service("prod-payment"')]
    dashboard = ts[ts.index('service("prod-dashboard"'):]
    dashboard = dashboard[:dashboard.index("});")]
    assert _domains_of(registry) == [("api.agentnet.io.vn", "8000")]
    assert _domains_of(dashboard) == [("agentnet.io.vn", "8080"), ("dashboard.agentnet.io.vn", "8080")]
    assert ts.count("domains:") == 2
    # every declared domain, anywhere in the file
    assert sorted(re.findall(r'\{ domain: "([^"]+)", port: (\d+) \}', ts)) == [
        ("agentnet.io.vn", "8080"),
        ("api.agentnet.io.vn", "8000"),
        ("dashboard.agentnet.io.vn", "8080"),
    ]
    for retired in ("www.agentnet.io.vn", "payment.agentnet.io.vn", "staging.agentnet.io.vn"):
        assert retired not in ts, retired
    # the port a domain routes to must be the port the service listens on
    assert 'PORT: "8000"' in registry and 'PORT: "8080"' in dashboard
    for exposure in ("tcp:", "tcpProxies", "serviceDomains", "customDomains"):
        assert exposure not in ts, exposure
    for private in ("prod-payment", "prod-worker", "prod-postgres", "prod-redis"):
        start = ts.index(f'service("{private}"')
        end = ts.index("});", start)
        assert "domains:" not in ts[start:end], f"{private} must stay private"


def test_production_iac_admits_only_the_canonical_ui_origin():
    """The registry admits exactly the canonical public UI origin (the apex);
    the dashboard compatibility host is redirected at the edge and admitted
    nowhere. Payment is private and admits no browser origin, but still gets
    the explicit list it refuses to start without. Never "*", never a
    Railway-generated domain."""
    ts = _prod_ts()
    assert 'const PUBLIC_UI_ORIGIN = "https://agentnet.io.vn";' in ts
    assert 'const PRIVATE_ONLY_CORS_ORIGIN = "http://prod-dashboard.railway.internal:8080";' in ts
    assert "PUBLIC_DASHBOARD_ORIGIN" not in ts
    assert '"https://dashboard.agentnet.io.vn"' not in ts
    registry = ts[ts.index('service("prod-registry"'):ts.index('service("prod-payment"')]
    payment = ts[ts.index('service("prod-payment"'):ts.index('service("prod-worker"')]
    assert "CORS_ALLOWED_ORIGINS: PUBLIC_UI_ORIGIN," in registry
    assert "CORS_ALLOWED_ORIGINS: PRIVATE_ONLY_CORS_ORIGIN," in payment
    assert ts.count("CORS_ALLOWED_ORIGINS:") == 2
    assert '"*"' not in ts and "up.railway.app" not in ts


# ── the production validator's log scan (Phase 7 §35) ────────────────────────

def _scan(log_text, values=None):
    from deploy.production.validate import Report, scan_logs_for_secrets

    report = Report()
    scan_logs_for_secrets(report, log_text, values)
    return next(c for c in report.checks if c["id"] == "L01")


def _bait() -> str:
    """A credential-shaped value, generated rather than written down.

    A literal here is what GitGuardian and the repository's own scanner exist
    to refuse, and "it is only bait" is exactly the argument that lets a real
    one through later. Generating it also strengthens the assertions below: a
    random value cannot be absent from a message by coincidence.
    """
    import uuid

    return uuid.uuid4().hex


def test_log_scan_finds_a_leak_without_being_told_any_secret():
    """The operator running this is forbidden to retrieve production secrets.
    If the scan only worked with values in hand, "I have none" would silently
    turn it into a check that always passes."""
    leaked = _bait()
    for line in (
        f"boot REDIS_PASSWORD={leaked} here",
        f'{{"jwt_secret_key": "{leaked}"}}',
        f"INTERNAL_WORKER_TOKEN: {leaked}",
    ):
        assert not _scan(line)["ok"], line


def test_log_scan_accepts_what_a_correct_log_looks_like():
    ok = _scan("POSTGRES_PASSWORD=***\nJWT_SECRET_KEY: <hidden>\nSMTP_PASSWORD=[REDACTED]\nstarted")
    assert ok["ok"], ok["detail"]


def test_log_scan_never_prints_the_value_it_matched():
    leaked = _bait()
    detail = _scan(f"REDIS_PASSWORD={leaked}")["detail"]
    assert "REDIS_PASSWORD" in detail and leaked not in detail


def test_log_scan_still_honours_explicit_values_when_an_operator_has_them():
    value = _bait()
    assert not _scan(f"the token is {value}", {"JWT_SECRET_KEY": value})["ok"]
    assert _scan("nothing here", {"JWT_SECRET_KEY": value})["ok"]


def test_the_validators_canary_address_is_acceptable_to_the_api_it_validates():
    """A canary address the API rejects on SYNTAX proves nothing about policy.

    The first live production run used `@agentnet.invalid`, which
    email-validator refuses as a special-use reserved name. Registration
    answered 422 from Pydantic before reaching the delivery check, and the
    validator recorded a production failure that was entirely its own. The
    canary must be refused for the RIGHT reason (delivery disabled -> 503) or
    not at all.
    """
    import pathlib
    import sys

    from pydantic import BaseModel, EmailStr

    root = str(pathlib.Path(__file__).resolve().parent.parent / "services" / "registry")
    if root not in sys.path:
        sys.path.insert(0, root)

    from deploy.production.validate import CANARY_DOMAIN, CANARY_PREFIX

    class _Addr(BaseModel):
        email: EmailStr

    # Must validate — otherwise the smoke test can never reach the code it tests.
    _Addr(email=f"{CANARY_PREFIX}-abc123@{CANARY_DOMAIN}")

    # And the domains that caused the original failure must stay rejected, so
    # this test fails loudly if someone "tidies" the canary back to one of them.
    import pytest as _pytest

    for bad in ("agentnet.invalid", "agentnet.test", "agentnet.localhost"):
        with _pytest.raises(Exception):
            _Addr(email=f"{CANARY_PREFIX}-abc123@{bad}")


# ── the production security suite (Phase 7 §38, §39) ─────────────────────────

def test_security_probes_are_all_anonymous_or_deliberately_invalid():
    """Every §38 probe must be safe to run against real production.

    A probe that mutated state, or that needed a real account, would either
    damage production or be impossible here -- registration is fail-closed
    while delivery is disabled, and forcing an account through a direct
    database write is the move that would invalidate the whole result.
    """
    import inspect

    from deploy.production import validate as v

    src = inspect.getsource(v.check_security)
    # No credential is ever supplied except a deliberately invalid one.
    assert 'token="not-a-real-token"' in src
    # The only POSTs are to routes that must REFUSE an anonymous caller, or
    # that are refused for another reason (malformed / oversized / rate limit).
    assert "/v1/agents/" in src and "/v1/auth/user/register" in src
    # Nothing deletes or patches.
    for verb in ('"DELETE"', '"PATCH"', '"PUT"'):
        assert verb not in src, f"{verb} is not production-safe in an anonymous probe"


def test_redis_auth_check_needs_both_halves_to_mean_anything():
    """R01 alone can pass because Redis is DOWN. R02 is what excludes that.

    This is the ADR-0008 D11 lesson encoded: during bring-up Redis served with
    no password while its config said otherwise, and only a real connection
    revealed it. A refusal check that cannot tell 'refused' from 'unreachable'
    would have reported PASS on an open Redis.
    """
    import inspect

    from deploy.production import validate as v

    src = inspect.getsource(v.check_redis_auth)
    assert '"R01"' in src and '"R02"' in src
    assert "may be a false pass" in src
    # The password is used, never recorded or printed.
    assert "report.record" not in src


def test_security_checks_are_wired_into_the_run():
    """A check that exists but is never called is worse than no check."""
    import inspect

    from deploy.production import validate as v

    main_src = inspect.getsource(v.main)
    assert "check_security(" in main_src
    assert "check_redis_auth(" in main_src
