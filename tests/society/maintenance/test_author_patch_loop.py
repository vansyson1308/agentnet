"""AuthorPatch: front-loaded context, a per-try read cap, in-try refusals and
structural per-turn telemetry.

Live 2026-10-08 (staging, 14 cases): AuthorPatch spent ~9 of 10 turns on reads
and submitted empty or QA-failing patches that were only discovered after the
try ended. The target files now arrive in the first message, reads are capped
per try, and an empty / whitespace-only / unparsable attempt is refused as a
tool result the model sees while it still has turns."""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid

from services.registry.app.maintenance import activities as act
from services.registry.app.maintenance import harness as h
from services.registry.app.maintenance.taxonomy import ActivityKind
from services.registry.app.society.engineering import workspace as ws_mod

SPEC = act.SPECS[ActivityKind.AUTHOR_PATCH]
SUBMIT = {"action": "submit", "result": {"summary": "link the landing CTA to /register"}}


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path, files):
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    ws = ws_mod.Workspace(uuid.uuid4(), tmp_path, "main", _git(tmp_path, "rev-parse", "HEAD"), tmp_path)
    return h.AttemptState(ws=ws, files_allowed=list(files), test_targets=[], max_test_runs=1, test_timeout=30, page_bytes=4000)


def _run(script, tools, **kw):
    model = act.ScriptedActivityModel(script)
    res = asyncio.run(act.run_activity(SPEC, {"plan": "x"}, model=model, tools=tools, **kw))
    return res, model


def test_reads_beyond_the_cap_are_refused_and_never_executed():
    executed = []

    def read(args):
        executed.append(args)
        return {"lines": ["x"]}

    script = iter([{"action": "read_range", "args": {"path": "a", "start_line": i}} for i in range(1, 6)] + [SUBMIT])
    res, model = _run(lambda m: next(script), {n: read for n in SPEC.tools}, max_read_calls=3)
    assert res.ok and len(executed) == 3
    results = [m["content"] for m in model.calls[-1] if m["content"].startswith("TOOL_RESULT")]
    assert act.READ_BUDGET_DIRECTIVE in results[2] and all('"read_budget"' in r for r in results[3:])
    assert [t.get("read") for t in res.turn_log[:5]] == [True] * 5 and res.turn_log[-1]["action"] == "submit"


def test_a_refused_submit_is_a_tool_result_and_the_try_continues():
    checks = iter([{"error": "unchanged", "code": "empty_patch"}, None])
    script = iter([SUBMIT, SUBMIT])
    res, model = _run(lambda m: next(script), {}, submit_check=lambda out: next(checks))
    assert res.ok and res.turns == 2 and res.turn_log[0]["refused"] == "empty_patch"
    assert "TOOL_RESULT submit (refused" in model.calls[-1][-1]["content"]


def test_a_refusal_on_the_final_turn_fails_with_its_structural_code():
    res, _ = _run(lambda m: SUBMIT, {}, max_turns=2, submit_check=lambda out: {"error": "no", "code": "syntax_error"})
    assert not res.ok and res.error_class == "syntax_error" and res.turns == 2


def test_turn_log_is_structural_only():
    secret_text = "PAGE TEXT THAT MUST NOT BE LOGGED"
    script = iter([{"action": "read_range", "args": {"path": "a"}}, SUBMIT])
    res, _ = _run(lambda m: next(script), {n: (lambda a: {"lines": [secret_text]}) for n in SPEC.tools})
    assert secret_text not in json.dumps(res.turn_log)
    assert set(res.turn_log[0]) == {"turn", "action", "tokens_in", "tokens_out", "bytes", "ok", "read"} and res.turn_log[0]["bytes"] > 0


def test_apply_patch_refuses_python_that_does_not_parse_and_writes_nothing(tmp_path):
    state = _repo(tmp_path, {"app/x.py": "A = 1\n"})
    tools = h.builder_tools(state)
    out = tools["apply_patch"]({"files": [{"path": "app/x.py", "operations": [{"op": "replace_exact", "old": "A = 1", "new": "A = ("}]}]})
    assert out["code"] == "syntax_error" and not out["applied"] and out["syntax_errors"][0].startswith("app/x.py:")
    assert (tmp_path / "app/x.py").read_text() == "A = 1\n" and state.patches_applied == 0
    assert tools["apply_patch"]({"files": [{"path": "app/x.py", "operations": [{"op": "replace_exact", "old": "A = 1", "new": "A = 2"}]}]})["applied"]


def test_submit_check_refuses_empty_and_whitespace_only_attempts(tmp_path):
    state = _repo(tmp_path, {"app/x.py": "A = 1\n"})
    check = h.submit_check(state)
    assert check({})["code"] == "empty_patch"
    (tmp_path / "app/x.py").write_text("A = 1\n\n", encoding="utf-8")
    assert check({})["code"] == "format_only"
    (tmp_path / "app/x.py").write_text("A = (\n", encoding="utf-8")
    assert check({})["code"] == "syntax_error"
    (tmp_path / "app/x.py").write_text("A = 2\n", encoding="utf-8")
    assert check({})["code"] == "tests_not_run", "a real change is submitted only once its acceptance tests ran green on it"
    state.test_runs = state.max_test_runs
    result = {}
    assert check(result) is None and result["tests_unverified"] is True, "budget spent: allowed, flagged"


def test_target_file_context_is_whole_windowed_bounded_and_contained(tmp_path):
    big = "".join(f"line_{i} = {i}\n" for i in range(1, 2001))
    for rel, text in {"app/small.py": "A = 1\n", "app/big.py": big, "tests/test_x.py": "pass\n"}.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    files = ["app/small.py", "app/big.py", "tests/test_x.py", "../outside.py", "app/missing.py"]
    ctx = h.target_file_context(tmp_path, files, ["the bug is at app/big.py:1000"])
    by = {e["path"]: e for e in ctx}
    assert set(by) == {"app/small.py", "app/big.py"} and by["app/small.py"]["content"] == "A = 1\n"
    (win,) = by["app/big.py"]["windows"]
    assert by["app/big.py"]["mode"] == "outline+windows" and win["start_line"] == 960 and "1000| line_1000 = 1000" in win["text"]
    tight = h.target_file_context(tmp_path, ["app/big.py", "app/small.py"], [], whole_file_bytes=10, total_bytes=200)
    assert [e["mode"] for e in tight] == ["omitted", "whole"]


def test_the_live_reader_summarises_where_turns_go():
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[3] / "deploy" / "railway" / "maintenance_live.py"
    spec = importlib.util.spec_from_file_location("maintenance_live_under_test", path)
    live = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)
    log = [{"turn": 1, "action": "read_range", "read": True, "bytes": 900}, {"turn": 2, "action": "apply_patch", "read": False, "bytes": 80},
           {"turn": 3, "action": "submit", "refused": "empty_patch"}, {"turn": 4, "action": "submit"}]
    out = live.turn_summary([{"kind": "author_patch", "status": "succeeded", "error_class": None, "turn_log": log}])
    row = out["author_patch:succeeded:-"]
    assert row["avg_reads"] == 1 and row["avg_read_bytes"] == 900 and row["avg_first_patch_turn"] == 2 and row["refused"] == {"empty_patch": 1}
