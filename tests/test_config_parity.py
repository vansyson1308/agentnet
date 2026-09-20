"""Configuration truth (Phase 3.1 §32): the Society runtime's environment
contract must be the same in code (``SocietySettings``), ``.env.example``,
the staging Compose file and the deployment architecture document.

* every ``SOCIETY_*`` name the code reads is documented in ``.env.example``
  (assignable line, or comment-only for the two secret names);
* every externally necessary Phase-3 deployment setting is exposable through
  ``docker-compose.staging.yml`` and named in ``docs/DEPLOYMENT_ARCHITECTURE.md``;
* secrets (installation token, App private key, model key) never appear as
  compose literals — only the model key pass-through that existed before;
* the dangerous flags default OFF in staging and no stale claim about a
  baked-in ``--forwarded-allow-ips '*'`` survives.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
CONFIG = REPO / "services/registry/app/society/config.py"
SOCIETY_PKG = REPO / "services/registry/app/society"
ENV_EXAMPLE = REPO / ".env.example"
STAGING = REPO / "docker-compose.staging.yml"
DEPLOY_DOC = REPO / "docs/DEPLOYMENT_ARCHITECTURE.md"

# Read only inside services/registry/app/society/github_credentials.py; must
# never be assignable in .env.example nor present in any compose file.
SECRET_ONLY = {"SOCIETY_GITHUB_TOKEN", "SOCIETY_GITHUB_APP_PRIVATE_KEY_PEM"}
# Not a setting at all (hard OFF in code).
NOT_A_SETTING = {"SOCIETY_PRODUCTION_DEPLOY_ENABLED"}
# Legacy alias kept for backwards compatibility in code; the contract uses the new name.
LEGACY_ALIASES = {"SOCIETY_MODEL_JSON_SCHEMA"}

# Externally necessary Phase-3 deployment contract (what an operator must be
# able to set on a host without editing code).
DEPLOYMENT_CONTRACT = [
    "SOCIETY_RUNTIME_ENABLED", "SOCIETY_AUTONOMOUS_CODE_ENABLED", "SOCIETY_STAGING_DEPLOY_ENABLED",
    "SOCIETY_MODEL_PROVIDER", "SOCIETY_MODEL_NAME", "SOCIETY_MODEL_BASE_URL", "SOCIETY_MODEL_API_KEY",
    "SOCIETY_MODEL_OUTPUT_FORMAT", "SOCIETY_MODEL_FAST_NAME", "SOCIETY_MODEL_STRONG_NAME",
    "SOCIETY_DAILY_MODEL_BUDGET", "SOCIETY_MAX_CORRELATION_COST_USD", "SOCIETY_MAX_EXPERIMENT_COST_USD", "SOCIETY_MAX_PROMOTION_COST_USD",
    "SOCIETY_MAX_ENGINEERING_TURNS", "SOCIETY_MAX_REPO_READS_PER_RUN", "SOCIETY_MAX_REPO_BYTES_PER_RUN", "SOCIETY_MAX_SEARCH_RESULTS",
    "SOCIETY_MAX_AUTONOMOUS_CANDIDATES_PER_DAY", "SOCIETY_MAX_RED_CANDIDATES_PER_DAY", "SOCIETY_MAX_PROMOTIONS_PER_DAY",
    "SOCIETY_MAX_OPEN_AUTONOMOUS_PRS", "SOCIETY_MAX_FILES_PER_CANDIDATE", "SOCIETY_MAX_DIFF_LINES",
    "SOCIETY_PROMOTION_PROVIDER", "SOCIETY_AUTO_MERGE_ENABLED", "SOCIETY_MAX_AUTONOMOUS_MERGES_PER_DAY", "SOCIETY_GITHUB_REPOSITORY", "SOCIETY_GITHUB_BASE_BRANCH", "SOCIETY_GITHUB_API_URL",
    "SOCIETY_GITHUB_CREDENTIAL_PROVIDER", "SOCIETY_GITHUB_APP_ID", "SOCIETY_GITHUB_INSTALLATION_ID", "SOCIETY_GITHUB_APP_PRIVATE_KEY_FILE",
    "SOCIETY_DEPLOYMENT_PROVIDER", "SOCIETY_FITNESS_TEST_TIMEOUT_SECONDS",
    "SOCIETY_PROMOTION_LEASE_SECONDS", "SOCIETY_PROMOTION_MAX_ATTEMPTS", "SOCIETY_PROMOTION_POLL_INTERVAL_SECONDS",
    "SOCIETY_REPO_ROOT", "SOCIETY_WORKSPACE_ROOT", "SOCIETY_METRICS_PORT",
]


def _code_names() -> set:
    """Every SOCIETY_* environment name the runtime reads (config.py plus the
    worker's own process knobs such as SOCIETY_METRICS_PORT)."""
    names = set()
    for f in (CONFIG, SOCIETY_PKG / "worker.py"):
        names |= set(re.findall(r'"(SOCIETY_[A-Z0-9_]+)"', f.read_text(encoding="utf-8")))
    return names


def _env_example_lines() -> list:
    return ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()


def _assignable(name: str) -> bool:
    return any(re.match(rf"^{re.escape(name)}=", ln) for ln in _env_example_lines())


def _mentioned(name: str) -> bool:
    return any(name in ln for ln in _env_example_lines())


def _staging_society_env() -> dict:
    """The society-worker-staging environment block, parsed textually (no
    docker binary needed): KEY: ${KEY:-default} | "literal"."""
    text = STAGING.read_text(encoding="utf-8")
    block = text.split("society-worker-staging:", 1)[1].split("\n  dashboard-staging:", 1)[0]
    env = {}
    for m in re.finditer(r"^\s{6}([A-Z0-9_]+):\s*(.+?)\s*(?:#.*)?$", block, re.M):
        env[m.group(1)] = m.group(2).strip()
    return env


def test_every_setting_the_code_reads_is_documented_in_env_example():
    missing = []
    for name in sorted(_code_names() - NOT_A_SETTING - LEGACY_ALIASES):
        if name in SECRET_ONLY:
            assert _mentioned(name) and not _assignable(name), f"{name} must be documented as comment-only"
            continue
        if not _assignable(name):
            missing.append(name)
    assert not missing, f"settings read by config.py but not in .env.example: {missing}"


def test_deployment_contract_names_exist_in_code():
    unknown = [n for n in DEPLOYMENT_CONTRACT if n not in _code_names()]
    assert not unknown, f"contract names that config.py never reads: {unknown}"


def test_deployment_contract_is_exposable_through_staging_compose():
    env = _staging_society_env()
    missing = [n for n in DEPLOYMENT_CONTRACT if n not in env]
    assert not missing, f"Phase-3 settings missing from society-worker-staging: {missing}"
    # every value is either a host-overridable ${VAR:-default} or a deliberate literal
    for name in DEPLOYMENT_CONTRACT:
        v = env[name]
        assert v.startswith("${") or v.startswith('"') or re.match(r"^[A-Za-z0-9_./:-]+$", v), (name, v)


def test_deployment_contract_is_named_in_the_architecture_doc():
    doc = DEPLOY_DOC.read_text(encoding="utf-8")
    missing = [n for n in DEPLOYMENT_CONTRACT if n not in doc and not any(n.startswith(p) and p in doc for p in ("SOCIETY_MAX_", "SOCIETY_PROMOTION_", "SOCIETY_GITHUB_", "SOCIETY_MODEL_"))]
    assert not missing, f"names absent from docs/DEPLOYMENT_ARCHITECTURE.md: {missing}"


def test_staging_defaults_are_safe_and_secrets_are_never_compose_literals():
    env = _staging_society_env()
    assert env["SOCIETY_RUNTIME_ENABLED"] == "${SOCIETY_RUNTIME_ENABLED:-false}"
    assert env["SOCIETY_AUTONOMOUS_CODE_ENABLED"] == "${SOCIETY_AUTONOMOUS_CODE_ENABLED:-false}"
    assert env["SOCIETY_STAGING_DEPLOY_ENABLED"] == '"false"'
    assert env["SOCIETY_PROMOTION_PROVIDER"] == "${SOCIETY_PROMOTION_PROVIDER:-disabled}"
    assert env["SOCIETY_AUTO_MERGE_ENABLED"] == '"false"', "auto-merge is not host-overridable in staging"
    assert env["SOCIETY_DEPLOYMENT_PROVIDER"] == "disabled"
    assert env["SOCIETY_GITHUB_CREDENTIAL_PROVIDER"] == "${SOCIETY_GITHUB_CREDENTIAL_PROVIDER:-disabled}"
    assert env["SOCIETY_GITHUB_APP_PRIVATE_KEY_FILE"] == "${SOCIETY_GITHUB_APP_PRIVATE_KEY_FILE:-}", "a PATH pass-through only"
    for compose in REPO.glob("docker-compose*.yml"):
        text = compose.read_text(encoding="utf-8")
        for secret in SECRET_ONLY:
            assert not re.search(rf"^\s*{secret}\s*:", text, re.M), f"{compose.name} must not carry {secret}"
        assert "DEEPSEEK_API_KEY" not in text
        assert "-----BEGIN" not in text


@pytest.mark.parametrize("compose", sorted(p.name for p in REPO.glob("docker-compose*.yml")))
def test_no_stale_forwarded_allow_ips_claim(compose):
    text = (REPO / compose).read_text(encoding="utf-8")
    assert "--forwarded-allow-ips '*'" not in text and '--forwarded-allow-ips "*"' not in text, compose
