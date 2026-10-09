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
RUN = {"action": "run_tests", "args": {}}
RESCOPE = {"action": "needs_rescope", "result": {"reason": "file_outside_scope", "required_files": ["pkg/other.py"], "evidence": "the bug is elsewhere"}}


def _bench(tmp_path, script, task=None, repeat=1):
    repo, t = _repo(tmp_path)
    lines = []
    summary = asyncio.run(bench.bench([task or t], repo=str(repo), model=ScriptedActivityModel(script), budget=Decimal("1"), repeat=repeat, emit=lines.append))
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
    assert summary["config"]["effective_max_tokens_per_turn"] == bench.act.SPECS[bench.ActivityKind.AUTHOR_PATCH].max_tokens


def test_a_correct_worktree_that_was_never_submitted_is_not_delivered(tmp_path):
    summary, rows = _bench(tmp_path, [_patch("pkg/mod.py", "return x + x + 1", "return x + x"), "not json", "not json"])
    assert rows[0]["scored"] is True and rows[0]["submitted"] is False and rows[0]["result"] == "invalid_json"
    assert summary["delivered"] == 0 and summary["pass_at_1"] == 0.0 and summary["undelivered_correct"] == 1


def test_a_turn_budget_try_with_green_acceptance_tests_on_its_worktree_is_submitted_by_the_harness(tmp_path):
    read_diff = [{"action": "read_diff", "args": {}}] * 30
    summary, rows = _bench(tmp_path, [_patch("pkg/mod.py", "return x + x + 1", "return x + x"), RUN] + read_diff)
    assert rows[0]["auto_submitted"] is True and rows[0]["submitted"] is True and rows[0]["result"] == "pass" and rows[0]["test_runs"] == 1
    # never tested by the model: the harness runs the acceptance tests once itself (no model call)
    _, rows = _bench(tmp_path / "b", [_patch("pkg/mod.py", "return x + x + 1", "return 2 * x")] + read_diff)
    assert rows[0]["auto_submitted"] is True and rows[0]["result"] == "pass" and rows[0]["test_runs"] == 1
    # green on an EARLIER worktree is not enough: the changed worktree is red, so nothing is submitted
    _, rows = _bench(tmp_path / "c", [_patch("pkg/mod.py", "return x + x + 1", "return x + x"), RUN, _patch("pkg/mod.py", "return x + x", "return x - x")] + read_diff)
    assert rows[0]["auto_submitted"] is False and rows[0]["result"] == "turn_budget" and rows[0]["test_runs"] == 2


def test_a_red_worktree_is_refused_with_its_failing_tests_and_a_green_one_is_accepted(tmp_path):
    script = [_patch("pkg/mod.py", "return x + x + 1", "return x + x + 2"), SUBMIT, SUBMIT, _patch("pkg/mod.py", "return x + x + 2", "return x + x"), SUBMIT]
    summary, rows = _bench(tmp_path, script)
    assert rows[0]["actions"] == ["apply_patch", "submit!tests_failing", "submit!tests_failing", "apply_patch", "submit"]
    assert rows[0]["result"] == "pass" and rows[0]["test_runs"] == 2, "the harness tested each new worktree once, never the same one twice"
    assert rows[0]["tests_unverified"] is False


def test_repeats_give_a_per_task_matrix_pass_at_1_and_pass_at_k(tmp_path):
    good = [_patch("pkg/mod.py", "return x + x + 1", "return x + x"), RUN, SUBMIT]
    summary, rows = _bench(tmp_path, [RESCOPE] + good + [RESCOPE], repeat=3)
    assert [r["rep"] for r in rows] == [0, 1, 2]
    assert summary["per_task"] == {"double": {"delivered": 1, "runs": ["wrong_file", "pass", "wrong_file"]}}
    assert summary["runs"] == 3 and summary["tasks_run"] == 1 and summary["pass_at_1"] == 0.333 and summary["pass_at_k"] == 1.0


def test_failures_are_classified_and_never_scored(tmp_path):
    _, rows = _bench(tmp_path, [_patch("pkg/other.py", "X = 1", "X = 2")] + [SUBMIT] * 12)
    assert rows[0]["result"] == "wrong_file" and "out_of_scope" in rows[0]["tool_codes"]
    _, rows = _bench(tmp_path / "b", [_patch("pkg/mod.py", "return x + x + 1", "return x + x + 2"), RUN] + [SUBMIT] * 12)
    assert rows[0]["result"] == "tests_failing" and rows[0]["submitted"] is False, "a red worktree is never submitted while test runs are left"


def test_only_a_live_model_produces_a_bench_result(tmp_path, monkeypatch):
    monkeypatch.setenv("SOCIETY_MODEL_PROVIDER", "scripted")
    monkeypatch.setenv("POSTGRES_HOST", "db.example")
    assert bench.main(["--repo", str(tmp_path)]) == 2
    assert bench.main(["--repo", str(tmp_path)], model=ScriptedActivityModel([])) == 2, "scripted output is refused without --allow-scripted"
    assert os.environ["POSTGRES_HOST"] == "db.example", "a refused run leaves the caller's environment alone"
