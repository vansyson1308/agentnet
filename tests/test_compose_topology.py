"""Compose environment isolation, proven on the RENDERED configuration.

`docker compose config --format json` is the client-side normalisation Docker
itself uses (no daemon needed): it resolves project name, interpolation,
merge of multiple -f files, resource names and labels. Compose scopes
`up`/`down`/`ps` by the project label it stamps on resources, so ownership is
fully determined by (project name, container_name, network/volume names) —
exactly what these tests assert.

Properties guaranteed here:
* `docker-compose.yml` is the LOCAL project `agentnet-local` and owns its own
  Postgres/Redis;
* `docker-compose.staging.yml` is the standalone project `agentnet-staging`,
  owns only `agentnet-staging-*` containers plus its own network/volume,
  never a Postgres/Redis, and fails to render without managed-infra env;
* the two projects share NO container, network or volume name, so
  `docker compose -f docker-compose.staging.yml down` cannot stop or remove
  anything local (or anything from the retired production overlay);
* the opt-in shared-infra overlay only attaches an EXTERNAL network;
* legacy VPS artifacts are quarantined and refuse to run.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCKER = shutil.which("docker")

LOCAL_FILE = "docker-compose.yml"
STAGING_FILE = "docker-compose.staging.yml"
OVERLAY_FILE = "docker-compose.staging.shared-infra.yml"
DEMO_FILE = "docker-compose.demo.yml"
LEGACY_PROD = "deploy/legacy-vps/docker-compose.prod.yml"
LEGACY_DIR = ROOT / "deploy" / "legacy-vps"

# Placeholder values so the staging project renders; they are never real.
STAGING_ENV = {
    "POSTGRES_HOST": "db.staging.invalid",
    "POSTGRES_USER": "staging_user",
    "POSTGRES_PASSWORD": "placeholder-render-only",
    "REDIS_HOST": "redis.staging.invalid",
    "REDIS_PASSWORD": "placeholder-render-only",
    "JWT_SECRET_KEY": "placeholder-render-only",
    "FLASK_SECRET_KEY": "placeholder-render-only",
    "CORS_ALLOWED_ORIGINS": "https://staging.invalid",
}
STAGING_REQUIRED = ["POSTGRES_HOST", "POSTGRES_USER", "POSTGRES_PASSWORD", "REDIS_HOST", "REDIS_PASSWORD", "JWT_SECRET_KEY", "CORS_ALLOWED_ORIGINS", "FLASK_SECRET_KEY"]

EXPECTED_LOCAL_CONTAINERS = {
    "agentnet-postgres",
    "agentnet-redis",
    "agentnet-registry",
    "agentnet-payment",
    "agentnet-worker",
    "agentnet-society-worker",
    "agentnet-simulation",
    "agentnet-jaeger",
    "agentnet-dashboard",
}
EXPECTED_STAGING_SERVICES = {"registry-staging", "payment-staging", "worker-staging", "society-worker-staging", "dashboard-staging"}


def _compose(files, *, env=None, extra_args=(), check=True):
    if DOCKER is None:
        pytest.skip("docker CLI not installed: rendered-config checks need `docker compose config` (client-side; no daemon required)")
    cmd = [DOCKER, "compose"]
    for f in files:
        cmd += ["-f", str(ROOT / f)]
    cmd += list(extra_args)
    merged = {k: v for k, v in os.environ.items() if not k.startswith(("POSTGRES_", "REDIS_", "JWT_", "SOCIETY_", "CORS_", "FLASK_", "SHARED_"))}
    merged.update(env or {})
    proc = subprocess.run(cmd, cwd=ROOT, env=merged, capture_output=True, text=True)
    if proc.returncode != 0 and "is not a docker command" in proc.stderr:
        pytest.skip("docker compose plugin not installed")
    if check:
        assert proc.returncode == 0, proc.stderr
    return proc


def render(files, *, env=None):
    proc = _compose(files, env=env, extra_args=["config", "--format", "json"])
    return json.loads(proc.stdout)


def containers(cfg):
    return {svc.get("container_name") or f"<{name}>" for name, svc in cfg["services"].items()}


def env_of(cfg, service):
    return {k: ("" if v is None else str(v)) for k, v in (cfg["services"][service].get("environment") or {}).items()}


@pytest.fixture(scope="module")
def local_cfg():
    return render([LOCAL_FILE])


@pytest.fixture(scope="module")
def staging_cfg():
    return render([STAGING_FILE], env=STAGING_ENV)


# ── local project ──────────────────────────────────────────────────────


def test_local_is_an_explicit_project_owning_its_own_infra(local_cfg):
    assert local_cfg["name"] == "agentnet-local"
    assert containers(local_cfg) == EXPECTED_LOCAL_CONTAINERS
    assert {"postgres", "redis"} <= set(local_cfg["services"])
    nets = local_cfg["networks"]
    assert set(nets) == {"agentnet-network"} and not nets["agentnet-network"].get("external")
    assert nets["agentnet-network"]["name"] == "agentnet-local"
    # pinned volume names keep pre-rename developer data reachable
    assert {v["name"] for v in local_cfg["volumes"].values()} == {"agentnet_postgres_data", "agentnet_redis_data", "agentnet_jaeger_data"}
    for name in containers(local_cfg):
        assert name.startswith("agentnet-") and not name.startswith("agentnet-staging-"), name


def test_demo_overlay_stays_inside_the_local_project():
    cfg = render([LOCAL_FILE, DEMO_FILE])
    assert cfg["name"] == "agentnet-local"
    # the demo overlay parks jaeger behind the `tracing` profile, so it is not rendered
    assert containers(cfg) == (EXPECTED_LOCAL_CONTAINERS - {"agentnet-jaeger"}) | {"agentnet-gateway"}
    assert not any(n.startswith("agentnet-staging-") for n in containers(cfg))


# ── staging project ────────────────────────────────────────────────────


def test_staging_is_a_standalone_project_with_only_staging_resources(staging_cfg):
    assert staging_cfg["name"] == "agentnet-staging"
    assert set(staging_cfg["services"]) == EXPECTED_STAGING_SERVICES
    for name in containers(staging_cfg):
        assert name.startswith("agentnet-staging-"), name
    assert not any(s in staging_cfg["services"] for s in ("postgres", "redis", "jaeger")), "staging must not own shared infrastructure"
    for net_key, net in staging_cfg["networks"].items():
        assert not net.get("external"), f"standalone staging must not reference external networks ({net_key})"
        assert net["name"].startswith("agentnet-staging_"), net
    for vol in staging_cfg["volumes"].values():
        assert vol["name"].startswith("agentnet-staging_"), vol
    for svc in staging_cfg["services"].values():
        assert not svc.get("privileged")
        for vol in svc.get("volumes") or []:
            assert "docker.sock" not in json.dumps(vol)


def test_staging_and_local_share_no_resource_names(local_cfg, staging_cfg):
    assert containers(local_cfg) & containers(staging_cfg) == set()
    assert {n["name"] for n in local_cfg["networks"].values()} & {n["name"] for n in staging_cfg["networks"].values()} == set()
    assert {v["name"] for v in local_cfg["volumes"].values()} & {v["name"] for v in staging_cfg["volumes"].values()} == set()
    assert local_cfg["name"] != staging_cfg["name"]


def test_staging_down_cannot_target_local_or_legacy_prod_containers(staging_cfg, local_cfg):
    """Compose removes only resources labelled with its own project. With a
    distinct project name AND disjoint container_names there is no path by
    which `docker compose -f docker-compose.staging.yml down` reaches a
    local or legacy-production container."""
    legacy = yaml.safe_load((ROOT / LEGACY_PROD).read_text())
    legacy_containers = {svc.get("container_name") for svc in legacy["services"].values() if svc.get("container_name")}
    assert legacy_containers, "legacy prod overlay should still declare container names"
    assert containers(staging_cfg).isdisjoint(legacy_containers)
    assert containers(staging_cfg).isdisjoint(containers(local_cfg))
    assert legacy.get("name") == "agentnet-legacy-prod" and legacy["name"] not in (staging_cfg["name"], local_cfg["name"])


def test_staging_down_dry_run_when_daemon_available():
    if DOCKER is None:
        pytest.skip("docker CLI not installed")
    if subprocess.run([DOCKER, "info"], capture_output=True).returncode != 0:
        pytest.skip("docker daemon unavailable: `--dry-run down` needs the Engine API; ownership is proven by the rendered-config tests above")
    proc = _compose([STAGING_FILE], env=STAGING_ENV, extra_args=["--dry-run", "down"], check=False)
    out = proc.stdout + proc.stderr
    for name in EXPECTED_LOCAL_CONTAINERS:
        assert name not in out, out


@pytest.mark.parametrize("missing", STAGING_REQUIRED)
def test_staging_fails_to_render_without_managed_infra_env(missing):
    env = dict(STAGING_ENV)
    env.pop(missing)
    proc = _compose([STAGING_FILE], env=env, extra_args=["config"], check=False)
    assert proc.returncode != 0 and missing in proc.stderr, f"{missing} must be required: {proc.stderr[:200]}"


def test_staging_never_defaults_infra_to_local_container_names():
    text = (ROOT / STAGING_FILE).read_text()
    assert not re.search(r"POSTGRES_HOST:\s*(postgres|agentnet-postgres)\b", text)
    assert not re.search(r"REDIS_HOST:\s*(redis|agentnet-redis)\b", text)
    assert "agentnet_agentnet-network" not in text, "no implicit dependency on the old base-project network"


def test_staging_society_worker_is_off_by_default_and_isolated(staging_cfg):
    svc = staging_cfg["services"]["society-worker-staging"]
    env = env_of(staging_cfg, "society-worker-staging")
    assert env["SOCIETY_RUNTIME_ENABLED"] == "false"
    assert env["SOCIETY_AUTONOMOUS_CODE_ENABLED"] == "false"
    assert env["SOCIETY_STAGING_DEPLOY_ENABLED"] == "false"
    assert "SOCIETY_PRODUCTION_DEPLOY_ENABLED" not in env, "production deploy is not a setting"
    assert env["SOCIETY_MODEL_PROVIDER"] == "scripted" and env["SOCIETY_MODEL_API_KEY"] == ""
    assert env["POSTGRES_DB"] == "agentnet_staging" and env["ENVIRONMENT"] == "staging"
    assert env["POSTGRES_HOST"] == STAGING_ENV["POSTGRES_HOST"]
    assert not svc.get("ports"), "society metrics/ports must never be published"
    assert svc.get("healthcheck")
    assert any(v.get("source") == "society_workspaces" for v in svc["volumes"])


def test_staging_registry_operator_bootstrap_from_env_only(staging_cfg):
    env = env_of(staging_cfg, "registry-staging")
    assert env["SOCIETY_OPERATOR_BOOTSTRAP_EMAILS"] == ""
    assert "SOCIETY_MODEL_API_KEY" not in env, "the API never needs the model credential"
    assert env["SOCIETY_RUNTIME_ENABLED"] == "false"


def test_shared_infra_overlay_only_attaches_an_external_network():
    env = {**STAGING_ENV, "SHARED_INFRA_NETWORK": "some-existing-network", "POSTGRES_HOST": "shared-postgres", "REDIS_HOST": "shared-redis"}
    cfg = render([STAGING_FILE, OVERLAY_FILE], env=env)
    assert cfg["name"] == "agentnet-staging"
    assert set(cfg["services"]) == EXPECTED_STAGING_SERVICES
    shared = cfg["networks"]["shared-infra"]
    assert shared["external"] is True and shared["name"] == "some-existing-network"
    assert cfg["networks"]["staging"]["name"] == "agentnet-staging_staging" and not cfg["networks"]["staging"].get("external")
    for name in ("registry-staging", "payment-staging", "worker-staging", "society-worker-staging"):
        assert set(cfg["services"][name]["networks"]) == {"staging", "shared-infra"}
    assert not any(s in cfg["services"] for s in ("postgres", "redis"))
    proc = _compose([STAGING_FILE, OVERLAY_FILE], env=STAGING_ENV, extra_args=["config"], check=False)
    assert proc.returncode != 0 and "SHARED_INFRA_NETWORK" in proc.stderr


def test_all_compose_projects_have_distinct_explicit_names(local_cfg, staging_cfg):
    legacy = yaml.safe_load((ROOT / LEGACY_PROD).read_text())
    names = [local_cfg["name"], staging_cfg["name"], legacy["name"]]
    assert len(set(names)) == 3 and all(n.startswith("agentnet-") for n in names)


# ── local dev society worker ───────────────────────────────────────────


def test_dev_society_worker_publishes_no_ports_and_no_socket(local_cfg):
    svc = local_cfg["services"]["society-worker"]
    assert not svc.get("ports")
    assert "docker.sock" not in json.dumps(svc.get("volumes") or [])
    assert env_of(local_cfg, "society-worker")["SOCIETY_RUNTIME_ENABLED"] == "false"


def test_registry_image_has_git_for_builder_worktrees():
    dockerfile = (ROOT / "services/registry/Dockerfile").read_text()
    assert "git" in dockerfile.split("apt-get install")[1].split("&&")[0]


# ── legacy VPS quarantine ──────────────────────────────────────────────

ACTIVE_GLOBS = ["Makefile", "README.md", "CLAUDE.md", "CURRENT_STATE.md", "RELEASE.md", ".github/workflows/*.yml", "docs/*.md", "docs/adr/*.md", "deploy/*.sh", "deploy/*.py", "docker-compose*.yml", "examples/**/*.md", "sdk/**/*.md"]
LEGACY_NAMES = ["docker-compose.prod.yml", "runbook-prod.sh", "runbook-staging.sh", "setup-oracle.sh", "deploy/Caddyfile", "tunnel-config.yml"]


def _active_files():
    for pattern in ACTIVE_GLOBS:
        for p in ROOT.glob(pattern):
            if p.is_file() and "legacy" not in p.parts:
                yield p


def test_no_active_file_combines_base_and_staging_compose():
    bad = []
    pattern = re.compile(r"docker-compose\.yml\s+-f\s+docker-compose\.(staging|prod)\.yml")
    for p in _active_files():
        for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if pattern.search(line) and not re.search(r"retired|never|LEGACY|historic|was deployed", line, re.I):
                bad.append(f"{p.relative_to(ROOT)}:{n}")
    assert not bad, f"base+overlay composite projects are retired (mention them only as history): {bad}"


def test_no_active_file_references_legacy_vps_artifacts():
    bad = []
    for p in _active_files():
        text = p.read_text(errors="replace")
        for name in LEGACY_NAMES:
            for m in re.finditer(re.escape(name), text):
                line = text[: m.start()].count("\n") + 1
                snippet = text.splitlines()[line - 1]
                if "legacy-vps" in snippet or "LEGACY" in snippet:
                    continue  # an explicit pointer at the quarantine is fine
                bad.append(f"{p.relative_to(ROOT)}:{line}: {snippet.strip()[:80]}")
    assert not bad, "\n".join(bad)


def test_legacy_vps_scripts_refuse_to_run():
    scripts = sorted(LEGACY_DIR.glob("*.sh"))
    assert {s.name for s in scripts} == {"runbook-prod.sh", "runbook-staging.sh", "setup-oracle.sh"}
    for script in scripts:
        text = script.read_text()
        assert "LEGACY — DO NOT USE FOR CURRENT DEPLOYMENT" in text, script
        proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, cwd=ROOT, env={**os.environ, "AGENTNET_ALLOW_LEGACY_VPS": ""})
        assert proc.returncode == 64 and "REFUSING" in proc.stderr, (script, proc.returncode, proc.stderr[:200])
    prod = (ROOT / LEGACY_PROD).read_text()
    assert "LEGACY — DO NOT USE FOR CURRENT DEPLOYMENT" in prod and "name: agentnet-legacy-prod" in prod
    assert (LEGACY_DIR / "README.md").exists() and "DO NOT USE" in (LEGACY_DIR / "README.md").read_text()
