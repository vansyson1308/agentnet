"""The acceptance/base test subprocess gets an ALLOWLISTED environment, never the worker's.

Staging 2026-10-27: candidate 877a0ceb was rejected "acceptance test red on base" because
tests/test_bench.py raised SocietyConfigError -- the worker's own SOCIETY_AUTO_MERGE_ENABLED=true
(GitHub provider) reached the pytest subprocess, whose settings then refused to load. The worker's
configuration must never judge a candidate (nor reach code a model wrote): the same subprocess env
(qa.scrubbed_env) serves QA's acceptance run, the harness's targeted runs, the red-on-base and the
test_first gates."""

from __future__ import annotations

import pathlib
import subprocess
import uuid

from services.registry.app.maintenance import harness as mharness
from services.registry.app.society.engineering import qa
from services.registry.app.society.engineering.workspace import Workspace

WORKER_ENV = {
    "SOCIETY_AUTO_MERGE_ENABLED": "true",
    "SOCIETY_PROMOTION_PROVIDER": "github",
    "SOCIETY_NORTH_STAR_ENABLED": "true",
    "MAINTENANCE_HARNESS2_ENABLED": "true",
    "DATABASE_URL": "postgresql://agentnet:pw@db.internal:5432/agentnet",
    "REDIS_URL": "redis://:pw@redis.internal:6379/0",
    "RAILWAY_ENVIRONMENT_NAME": "staging",
    "A2A_SERVER_ENABLED": "true",
}

PROBE = '''import os

LEAKS = {leaks!r}


def test_the_worker_environment_does_not_reach_the_test():
    assert [k for k in LEAKS if k in os.environ] == []
    assert os.environ["SOCIETY_RUNTIME_ENABLED"] == "false" and os.environ["ENVIRONMENT"] == "development"
'''


def _ws(tmp_path: pathlib.Path) -> Workspace:
    root = tmp_path / "wt"
    (root / "tests").mkdir(parents=True)
    (root / "tests/test_env_probe.py").write_text(PROBE.format(leaks=sorted(WORKER_ENV)))
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return Workspace(candidate_id=uuid.uuid4(), path=root, branch="t", base_sha="", repo_root=root)


def test_scrubbed_env_is_an_allowlist(tmp_path, monkeypatch):
    for k, v in WORKER_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LANG", "C.UTF-8")
    env = qa.scrubbed_env(_ws(tmp_path))
    assert [k for k in WORKER_ENV if k in env] == []
    assert all(qa._TEST_ENV_PASS.match(k) for k in env if k not in qa.TEST_ENV_FIXED), sorted(env)
    assert env["PATH"] and env["LANG"] == "C.UTF-8" and env["PYTHONPATH"] == str(tmp_path / "wt")
    assert (env["SOCIETY_RUNTIME_ENABLED"], env["SOCIETY_AUTONOMOUS_CODE_ENABLED"], env["ENVIRONMENT"]) == ("false", "false", "development")


def test_the_acceptance_and_base_runs_never_see_the_worker_config(tmp_path, monkeypatch):
    for k, v in WORKER_ENV.items():
        monkeypatch.setenv(k, v)
    ws = _ws(tmp_path)
    check = qa._run_acceptance(ws, ["tests/test_env_probe.py"], 120)
    assert check.passed, check.detail
    run = mharness.run_targeted_tests(ws, ["tests/test_env_probe.py"], timeout=120)  # red-on-base, test_first and the build loop
    assert run.passed, run.tail


def test_the_bench_test_loads_its_settings_with_the_worker_on_auto_merge(monkeypatch):
    """The ticket b3e45c77 case: tests/test_bench.py on the base revision, the worker configured for auto-merge."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    for k, v in WORKER_ENV.items():
        monkeypatch.setenv(k, v)
    ws = Workspace(candidate_id=uuid.uuid4(), path=repo, branch="main", base_sha="", repo_root=repo)
    run = mharness.run_targeted_tests(ws, ["tests/test_bench.py::test_a_fixed_task_scores_and_the_report_is_marked_not_live"], timeout=300)
    assert run.passed, run.tail[-1500:]
