"""The offline builder bench (scripts/bench): its task list, judge and runner.

The runner's own tests use a scripted model on a throw-away repository and are
reported ``live: false``; a scripted run is never a bench result."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from decimal import Decimal

from scripts.bench import run as bench
from services.registry.app.maintenance.activities import ScriptedActivityModel

BUGGY, FIXED = "def double(x):\n    return x + x + 1\n", "def double(x):\n    return x + x\n"
TEST = "from pkg.mod import double\n\n\ndef test_double():\n    assert double(2) == 4\n\n\ndef test_zero():\n    assert double(0) == 0\n"


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "mod.py").write_text(BUGGY)
    (repo / "pkg" / "other.py").write_text("X = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    (repo / "pkg" / "mod.py").write_text(FIXED)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_mod.py").write_text(TEST)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fix double")
    task = {"id": "double", "fix_sha": _git(repo, "rev-parse", "HEAD"), "files_allowed": ["pkg/mod.py"], "test_files": ["tests/test_mod.py"],
            "failing_tests": ["tests/test_mod.py::test_double", "tests/test_mod.py::test_zero"], "task": "double(x) must return 2x."}
    return repo, task


def _patch(path, old, new):
    return {"action": "apply_patch", "args": {"files": [{"path": path, "operations": [{"op": "replace_exact", "old": old, "new": new}]}]}}


SUBMIT = {"action": "submit", "result": {"summary": "double returns 2x"}}


def _bench(tmp_path, script, task=None):
    repo, t = _repo(tmp_path)
    lines = []
    summary = asyncio.run(bench.bench([task or t], repo=str(repo), model=ScriptedActivityModel(script), budget=Decimal("1"), emit=lines.append))
    return summary, [json.loads(x.split(" ", 2)[2]) for x in lines if x.startswith("BENCH TASK")]


def test_the_task_list_is_a_judge_of_real_commits():
    doc = json.loads(bench.TASKS.read_text(encoding="utf-8"))
    tasks = doc["tasks"]
    assert 12 <= len(tasks) <= 20 and len({t["id"] for t in tasks}) == len(tasks)
    for t in tasks:
        assert re.fullmatch(r"[0-9a-f]{40}", t["fix_sha"]) and t["failing_tests"] and 1 <= len(t["files_allowed"]) <= 5
        assert all(n.split("::")[0] in t["test_files"] for n in t["failing_tests"]), t["id"]
        assert not any("tests/" in p or p.split("/")[-1].startswith("test_") for p in t["files_allowed"]), t["id"]
        assert "@@" not in t["task"] and "+++" not in t["task"] and 80 <= len(t["task"]) <= 900, t["id"]


def test_a_fixed_task_scores_and_the_report_is_marked_not_live(tmp_path):
    summary, rows = _bench(tmp_path, [_patch("pkg/mod.py", "return x + x + 1", "return x + x"), {"action": "run_tests", "args": {}}, SUBMIT])
    assert rows[0]["result"] == "pass" and rows[0]["test_runs"] == 1 and rows[0]["patches"] == 1
    assert summary["live"] is False and summary["pass_at_1"] == 1.0 and summary["result_classes"] == {"pass": 1}


def test_failures_are_classified_and_never_scored(tmp_path):
    _, rows = _bench(tmp_path, [_patch("pkg/other.py", "X = 1", "X = 2")] + [SUBMIT] * 12)
    assert rows[0]["result"] == "wrong_file" and "out_of_scope" in rows[0]["tool_codes"]
    _, rows = _bench(tmp_path / "b", [_patch("pkg/mod.py", "return x + x + 1", "return x + x + 2"), SUBMIT])
    assert rows[0]["result"] == "tests_fail" and rows[0]["submitted"] is True


def test_only_a_live_model_produces_a_bench_result(tmp_path, monkeypatch):
    monkeypatch.setenv("SOCIETY_MODEL_PROVIDER", "scripted")
    monkeypatch.setenv("POSTGRES_HOST", "db.example")
    assert bench.main(["--repo", str(tmp_path)]) == 2
    assert bench.main(["--repo", str(tmp_path)], model=ScriptedActivityModel([])) == 2, "scripted output is refused without --allow-scripted"
    assert os.environ["POSTGRES_HOST"] == "db.example", "a refused run leaves the caller's environment alone"
