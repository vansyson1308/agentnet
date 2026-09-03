"""Guard rails for the society worker in the compose topologies.

* staging carries a society worker that is OFF by default, has no docker
  socket, publishes no ports and is pinned to the staging database;
* production compose has NO society worker and no society flags at all;
* the dev compose worker publishes no ports either.
"""

from __future__ import annotations

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text())


def _env(service: dict) -> dict:
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return {k: str(v) for k, v in env.items()}


def _society_services(compose: dict) -> dict:
    return {name: svc for name, svc in compose["services"].items() if "app.society.worker" in str(svc.get("command", ""))}


def test_staging_society_worker_is_off_by_default_and_isolated():
    compose = _load("docker-compose.staging.yml")
    workers = _society_services(compose)
    assert list(workers) == ["society-worker-staging"]
    svc = workers["society-worker-staging"]
    env = _env(svc)
    assert env["SOCIETY_RUNTIME_ENABLED"] == "${SOCIETY_RUNTIME_ENABLED:-false}"
    assert env["SOCIETY_AUTONOMOUS_CODE_ENABLED"] == "${SOCIETY_AUTONOMOUS_CODE_ENABLED:-false}"
    assert env["SOCIETY_STAGING_DEPLOY_ENABLED"] == "false"
    assert "SOCIETY_PRODUCTION_DEPLOY_ENABLED" not in env, "production deploy is not a setting"
    assert env["POSTGRES_DB"] == "agentnet_staging" and env["ENVIRONMENT"] == "staging"
    assert env["SOCIETY_MODEL_PROVIDER"] == "${SOCIETY_MODEL_PROVIDER:-scripted}"
    assert env["SOCIETY_MODEL_API_KEY"] == "${SOCIETY_MODEL_API_KEY:-}", "credential must come from the environment, never a literal"
    assert "ports" not in svc, "society metrics/ports must never be published"
    assert not svc.get("privileged")
    for vol in svc.get("volumes") or []:
        assert "docker.sock" not in str(vol)
    assert svc.get("healthcheck"), "worker needs a healthcheck for the runbook wait loop"
    assert "society_workspaces_staging" in (compose.get("volumes") or {})


def test_staging_registry_gets_operator_bootstrap_allowlist_only_from_env():
    compose = _load("docker-compose.staging.yml")
    env = _env(compose["services"]["registry-staging"])
    assert env["SOCIETY_OPERATOR_BOOTSTRAP_EMAILS"] == "${SOCIETY_OPERATOR_BOOTSTRAP_EMAILS:-}"
    assert "SOCIETY_MODEL_API_KEY" not in env, "the API never needs the model credential"


def test_production_compose_has_no_society_runtime():
    compose = _load("docker-compose.prod.yml")
    assert _society_services(compose) == {}
    text = (ROOT / "docker-compose.prod.yml").read_text()
    assert "SOCIETY_" not in text
    assert "docker.sock" not in text


def test_dev_society_worker_publishes_no_ports_and_no_socket():
    compose = _load("docker-compose.yml")
    workers = _society_services(compose)
    assert list(workers) == ["society-worker"]
    svc = workers["society-worker"]
    assert "ports" not in svc
    for vol in svc.get("volumes") or []:
        assert "docker.sock" not in str(vol)
    assert _env(svc)["SOCIETY_RUNTIME_ENABLED"] == "${SOCIETY_RUNTIME_ENABLED:-false}"


def test_registry_image_has_git_for_builder_worktrees():
    """The society worker runs from the registry image; the Builder needs git."""
    dockerfile = (ROOT / "services/registry/Dockerfile").read_text()
    assert "git" in dockerfile.split("apt-get install")[1].split("&&")[0]
