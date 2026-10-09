"""Best-of-N AuthorPatch (harness.author_patch) and the bench holdout boundary.

Scripted model on a throw-away repository (never live evidence)."""

from __future__ import annotations

import asyncio
import pathlib
import subprocess
import uuid
from decimal import Decimal

import pytest

from services.registry.app.maintenance import harness as h
from services.registry.app.maintenance.activities import ScriptedActivityModel
from services.registry.app.maintenance.config import MaintenanceSettings
from services.registry.app.maintenance.repo_tools import RepoToolError, RepoTools
from services.registry.app.society import repo_intel
from services.registry.app.society.engineering import workspace as ws_mod

RED = {"action": "apply_patch", "args": {"files": [{"path": "pkg/mod.py", "operations": [{"op": "replace_exact", "old": "x + x + 1", "new": "x + x + 2"}]}]}}
RESCOPE = {"action": "needs_rescope", "result": {"reason": "file_outside_scope", "required_files": ["pkg/other.py"], "evidence": "the bug is elsewhere"}}


def _ws(tmp_path) -> ws_mod.Workspace:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "mod.py").write_text("def double(x):\n    return x + x + 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_mod.py").write_text("from pkg.mod import double\n\n\ndef test_double():\n    assert double(2) == 4\n")
    (repo / "scripts" / "bench").mkdir(parents=True)
    (repo / "scripts" / "bench" / "holdout.json").write_text('{"tasks": [{"id": "secret-task", "task": "x + x + 1"}]}\n')
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    return ws_mod.Workspace(uuid.uuid4(), pathlib.Path(repo), "t", sha, pathlib.Path(repo))


def _run(ws, script, *, cap="1", samples="3", monkeypatch):
    monkeypatch.setenv("MAINTENANCE_BUILDER_SAMPLES", samples)
    model = ScriptedActivityModel(script)
    run = asyncio.run(h.author_patch(ws, {"task": "double(x) is 2x"}, files_allowed=["pkg/mod.py"], tests=["tests/test_mod.py::test_double"], model=model,
                                     settings=MaintenanceSettings(), test_timeout=60, cost_cap=Decimal(cap)))
    return run, model


def test_samples_share_one_cost_cap(tmp_path, monkeypatch):
    run, model = _run(_ws(tmp_path), [RED] + [{"action": "run_tests", "args": {}}] * 40, cap="0.0015", monkeypatch=monkeypatch)
    assert len(run.samples) == 1 and run.result.error_class == "cost_budget", "the first sample spent the shared cap: no second sample"
    assert run.result.cost_usd == Decimal("0.0002") * len(model.calls)


def test_a_rescope_answer_wins_over_later_failed_samples_and_each_sample_starts_from_the_base(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    run, model = _run(ws, [RESCOPE, RED, "not json", "not json", "not json", "not json"], monkeypatch=monkeypatch)
    assert [s["rescope"] for s in run.samples] == [True, False, False] and run.result.rescope["required_files"] == ["pkg/other.py"]
    assert model.temperatures[0] == 0.1 and model.temperatures[-1] == 0.8
    assert "sample:2" in [t["action"] for t in run.result.turn_log]


@pytest.mark.parametrize("path", ["scripts/bench/holdout.json", "./scripts/bench/holdout.json"])
def test_bench_holdout_is_never_in_any_tool_result(tmp_path, path):
    ws = _ws(tmp_path)
    tools = RepoTools(ws.path, page_bytes=4000)
    with pytest.raises(RepoToolError):
        tools.read_range(path)
    assert tools.search("secret-task")["matches"] == []
    with pytest.raises(repo_intel.RepoReadError):
        repo_intel.normalize_rel_path(path.lstrip("./"))
    listed = repo_intel.list_tree(ws.path, "scripts/bench", depth=2, max_entries=50).data["entries"]
    assert not any("holdout" in e["path"] for e in listed)
