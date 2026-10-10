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

import pytest

from scripts.bench import run as bench
from services.registry.app.maintenance.activities import ScriptedActivityModel

BUGGY, FIXED = "def double(x):\n    return x + x + 1\n", "def double(x):\n    return x + x\n"
TEST = "from pkg.mod import double\n\n\ndef test_double():\n    assert double(2) == 4\n\n\ndef test_zero():\n    assert double(0) == 0\n"


@pytest.fixture(autouse=True)
def _one_sample(monkeypatch):
    """The single-try semantics below; best-of-N has its own test."""
    monkeypatch.setenv("MAINTENANCE_BUILDER_SAMPLES", "1")


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


def _bench(tmp_path, script, task=None, repeat=1, path="maintenance"):
    repo, t = _repo(tmp_path)
    lines = []
    summary = asyncio.run(bench.bench([task or t], repo=str(repo), model=ScriptedActivityModel(script), budget=Decimal("1"), repeat=repeat, path=path, emit=lines.append))
    return summary, [json.loads(x.split(" ", 2)[2]) for x in lines if x.startswith("BENCH TASK")]


def test_the_task_list_is_a_judge_of_real_commits():
    tasks = bench.load_tasks(split="all")
    dev, holdout = bench.load_tasks(split="dev"), bench.load_tasks(split="holdout")
    assert 12 <= len(tasks) <= 40 and len({t["id"] for t in tasks}) == len(tasks) and len(dev) + len(holdout) == len(tasks)
    assert len(holdout) >= 4 and {t["split"] for t in holdout} == {"holdout"} and not any(t["id"] in bench.TASKS.read_text() for t in holdout)
    for t in tasks:
        assert re.fullmatch(r"[0-9a-f]{40}", t["fix_sha"]) and t["failing_tests"] and 1 <= len(t["files_allowed"]) <= 5
        assert all(n.split("::")[0] in t["test_files"] for n in t["failing_tests"]), t["id"]
        assert not any("tests/" in p or p.split("/")[-1].startswith("test_") for p in t["files_allowed"]), t["id"]
        assert "@@" not in t["task"] and "+++" not in t["task"] and 80 <= len(t["task"]) <= 900, t["id"]


def test_a_fixed_task_scores_and_the_report_is_marked_not_live(tmp_path):
    summary, rows = _bench(tmp_path, [_patch("pkg/mod.py", "return x + x + 1", "return x + x"), {"action": "run_tests", "args": {}}, SUBMIT])
    assert rows[0]["result"] == "pass" and rows[0]["test_runs"] == 1 and rows[0]["patches"] == 1
    assert summary["live"] is False and summary["pass_at_1"] == 1.0 and summary["result_classes"] == {"pass": 1}
    assert summary["config"]["effective_max_tokens_per_turn"] == 2500 and summary["config"]["builder_samples"] == 1


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
    assert summary["per_task"] == {"double": {"delivered": 1, "runs": ["wrong_file", "pass", "wrong_file"], "split": "dev"}}
    assert summary["runs"] == 3 and summary["tasks_run"] == 1 and summary["pass_at_1"] == 0.333 and summary["pass_at_k"] == 1.0


def test_failures_are_classified_and_never_scored(tmp_path):
    _, rows = _bench(tmp_path, [_patch("pkg/other.py", "X = 1", "X = 2")] + [SUBMIT] * 12)
    assert rows[0]["result"] == "wrong_file" and "out_of_scope" in rows[0]["tool_codes"]
    _, rows = _bench(tmp_path / "b", [_patch("pkg/mod.py", "return x + x + 1", "return x + x + 2"), RUN] + [SUBMIT] * 12)
    assert rows[0]["result"] == "tests_failing" and rows[0]["submitted"] is False, "a red worktree is never submitted while test runs are left"


def test_the_society_path_drives_the_same_harness_and_scores_the_same(tmp_path):
    good = [_patch("pkg/mod.py", "return x + x + 1", "return x + x"), RUN, SUBMIT]
    red = [_patch("pkg/mod.py", "return x + x + 1", "return x + x + 2"), RUN] + [SUBMIT] * 12
    for i, script in enumerate([good, red, [RESCOPE]]):
        _, m = _bench(tmp_path / f"m{i}", list(script))
        summary, s = _bench(tmp_path / f"s{i}", list(script), path="society")
        keys = ("result", "submitted", "scored", "test_runs", "patches", "actions")
        assert {k: s[0][k] for k in keys} == {k: m[0][k] for k in keys}
        assert summary["config"]["path"] == "society"


def test_a_run_is_best_of_n_delivered_by_the_harness_gate_never_picked_by_the_judge(tmp_path, monkeypatch):
    monkeypatch.setenv("MAINTENANCE_BUILDER_SAMPLES", "3")
    monkeypatch.setenv("MAINTENANCE_BUILDER_MAX_OUTPUT_TOKENS", "16000")
    red = [_patch("pkg/mod.py", "return x + x + 1", "return x + x + 2"), RUN] + [SUBMIT] * 8  # sample 1: all 10 turns, never green
    good = [_patch("pkg/mod.py", "return x + x + 1", "return x + x"), RUN, SUBMIT]  # sample 2 starts from the base again
    model = ScriptedActivityModel(red + good + [RESCOPE])
    repo, task = _repo(tmp_path)
    lines = []
    summary = asyncio.run(bench.bench([task], repo=str(repo), model=model, budget=Decimal("1"), emit=lines.append))
    row = json.loads(lines[0].split(" ", 2)[2])
    assert row["result"] == "pass" and row["samples"] == 2 and row["sample_results"] == ["tests_failing", "pass"], "stops at the first green sample"
    assert row["turns"] == 13 and row["cost_usd"] == str(Decimal("0.0002") * 13), "turns and cost add up over the samples"
    assert sorted(set(model.temperatures)) == [0.1, 0.5] and model.temperatures[-1] == 0.5 and len(model.calls) == 13
    assert summary["config"]["builder_samples"] == 3 and summary["config"]["sample_temperatures"] == [0.1, 0.5, 0.8]
    assert summary["config"]["effective_max_tokens_per_turn"] == 16000


def test_scores_per_split_the_aggregate_report_row_and_the_harness_merge_rule(tmp_path):
    repo, task = _repo(tmp_path)
    good = [_patch("pkg/mod.py", "return x + x + 1", "return x + x"), RUN, SUBMIT]
    tasks = [task, {**task, "id": "double-h", "split": "holdout"}]
    base = asyncio.run(bench.bench(tasks, repo=str(repo), model=ScriptedActivityModel(good + [RESCOPE]), budget=Decimal("1"), emit=lambda _: None))
    assert base["splits"] == {"dev": {"tasks": 1, "pass_at_1": 1.0, "pass_at_k": 1.0}, "holdout": {"tasks": 1, "pass_at_1": 0.0, "pass_at_k": 0.0}}
    row = bench.report_row(base, revision="r1", judge_revision="r1")
    assert row["per_task"]["double-h"] == {"split": "holdout", "delivered": 0, "runs": ["wrong_file"]} and task["task"] not in json.dumps(row)
    better = {**base, "splits": {**base["splits"], "holdout": {"pass_at_1": 0.5}}}
    assert bench.harness_verdict(base, better)["mergeable"] is True
    lost = {**better, "per_task": {**base["per_task"], "double": {"delivered": 0, "runs": ["wrong_file"] * 3}}}
    v = bench.harness_verdict({**base, "per_task": {**base["per_task"], "double": {"delivered": 3, "runs": ["pass"] * 3}}}, lost)
    assert v["regressed_3_to_0"] == ["double"] and v["mergeable"] is False
    assert bench.harness_verdict(base, base)["mergeable"] is False, "no holdout improvement, no merge"


def test_the_miner_turns_a_merged_fix_with_a_new_failing_test_into_a_candidate(tmp_path):
    from scripts.bench import mine

    repo, task = _repo(tmp_path)  # HEAD = "fix double" (fix + test) on top of base
    _git(repo, "checkout", "-qb", "fix", "HEAD")
    _git(repo, "checkout", "-q", "HEAD~1")
    _git(repo, "checkout", "-qB", "main")
    msg = "double(x) returned 2x+1: the arithmetic helper must return exactly twice its input for every integer, zero included"
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-qm", "unrelated")
    _git(repo, "checkout", "-q", "fix")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--amend", "-qm", msg)
    _git(repo, "checkout", "-q", "main")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "--no-ff", "-qm", "Merge pull request #1 from fix", "fix")
    cands = mine.mine(str(repo), "main~1", set(), ref="main")
    assert len(cands) == 1 and cands[0]["fix_sha"] == _git(repo, "rev-parse", "HEAD") and cands[0]["files_allowed"] == ["pkg/mod.py"]
    assert cands[0]["failing_tests"] == ["tests/test_mod.py::test_double", "tests/test_mod.py::test_zero"] and cands[0]["task"] == msg
    assert cands[0]["split"] in ("dev", "holdout") and mine.mine(str(repo), "main~1", {cands[0]["fix_sha"]}, ref="main") == []


def test_only_a_live_model_produces_a_bench_result(tmp_path, monkeypatch):
    monkeypatch.setenv("SOCIETY_MODEL_PROVIDER", "scripted")
    monkeypatch.setenv("POSTGRES_HOST", "db.example")
    assert bench.main(["--repo", str(tmp_path)]) == 2
    assert bench.main(["--repo", str(tmp_path)], model=ScriptedActivityModel([])) == 2, "scripted output is refused without --allow-scripted"
    assert os.environ["POSTGRES_HOST"] == "db.example", "a refused run leaves the caller's environment alone"
