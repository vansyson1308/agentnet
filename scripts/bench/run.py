"""Offline builder bench: how often does the maintenance builder loop fix a real task?

For each task in tasks.json: a worktree at ``fix_sha`` with ``files_allowed``
reverted to ``fix_sha^`` (the task's tests present, its fix absent). The
maintenance AuthorPatch loop (harness.builder_tools + activities.run_activity,
exactly as the kernel runs it) works the task with the configured model;
scripts/bench/score.py then judges the worktree.

    python scripts/bench/run.py --repo <checkout with the task commits> [--only id,id] [--json-out f]

Only a LIVE model is accepted (``--allow-scripted`` exists for the bench's own
tests and marks the report ``live: false``). The run stops at
``BENCH_BUDGET_USD`` (default 1). Nothing touches a database, a wallet or GitHub.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.bench.score import score  # noqa: E402
from services.registry.app.maintenance import activities as act  # noqa: E402
from services.registry.app.maintenance import harness as h  # noqa: E402
from services.registry.app.maintenance.config import MaintenanceSettings  # noqa: E402
from services.registry.app.maintenance.taxonomy import ActivityKind  # noqa: E402
from services.registry.app.society.config import SocietySettings  # noqa: E402
from services.registry.app.society.engineering import workspace as ws_mod  # noqa: E402

TASKS = pathlib.Path(__file__).with_name("tasks.json")
PATCH_PROTOCOL = ("apply_patch args: {files: [{path, operations: [{op: replace_exact|insert_after|insert_before|create|delete, "
                  "old/new | anchor/text | text}]}]} -- exact text, each old/anchor unique")
WRONG_FILE = ("out_of_scope", "protected", "path")


def _git(args: List[str], cwd: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def prepare(repo: str, task: dict, root: str) -> ws_mod.Workspace:
    """Worktree at fix_sha with the fix reverted, committed as the attempt's base."""
    path = os.path.join(root, task["id"])
    _git(["worktree", "add", "-q", "--detach", path, task["fix_sha"]], repo)
    _git(["checkout", task["fix_sha"] + "^", "--", *task["files_allowed"]], path)
    _git(["-c", "user.name=bench", "-c", "user.email=bench@localhost", "commit", "-q", "--no-verify", "-m", f"bench base: {task['id']}"], path)
    return ws_mod.Workspace(uuid.uuid5(uuid.NAMESPACE_URL, "bench:" + task["id"]), pathlib.Path(path), "bench", _git(["rev-parse", "HEAD"], path), pathlib.Path(repo))


def task_input(task: dict, ws: ws_mod.Workspace, ms: MaintenanceSettings) -> Dict[str, Any]:
    return {
        "task": {"trust": "bench_task", "text": task["task"]},
        "plan": {"files_allowed": task["files_allowed"], "acceptance_tests": task["failing_tests"], "base_sha": ws.base_sha},
        "patch_protocol": PATCH_PROTOCOL,
        "target_files": {"trust": "untrusted_repository_data", "files": h.target_file_context(ws.path, task["files_allowed"], [task["task"]])},
        "read_budget": f"at most {ms.builder_max_read_calls} read-tool calls this try; the target files are above",
    }


def _recording(tools: Dict[str, Any], codes: List[str]) -> Dict[str, Any]:
    def wrap(fn):
        def call(args):
            out = fn(args)
            if isinstance(out, dict) and out.get("code"):
                codes.append(str(out["code"]))
            return out
        return call
    return {name: wrap(fn) for name, fn in tools.items()}


def classify(res: act.ActivityResult, codes: List[str], scored: dict) -> str:
    if scored["passed"]:
        return "pass"
    if res.error_class in ("timeout", "turn_budget"):
        return res.error_class
    if any(str(t.get("action")) == "invalid:partial" for t in res.turn_log):
        return "truncated_output"
    if res.error_class == "syntax_error" or (codes and codes[-1] == "syntax_error"):
        return "syntax"
    if any(c in WRONG_FILE for c in codes) or res.rescope:
        return "wrong_file"
    if res.ok:
        return "tests_fail"
    return res.error_class or "no_answer"


async def run_task(task: dict, *, repo: str, root: str, model, ms: MaintenanceSettings, ss: SocietySettings, cost_cap: Decimal) -> Dict[str, Any]:
    ws = prepare(repo, task, root)
    state = h.AttemptState(ws=ws, files_allowed=list(task["files_allowed"]), test_targets=list(task["failing_tests"]),
                           max_test_runs=ms.max_test_runs_per_attempt, test_timeout=ss.qa_test_timeout_seconds, page_bytes=ms.read_page_bytes)
    codes: List[str] = []
    res = await act.run_activity(act.SPECS[ActivityKind.AUTHOR_PATCH], task_input(task, ws, ms), model=model, tools=_recording(h.builder_tools(state), codes),
                                 max_turns=ms.builder_max_turns, cost_cap=cost_cap, timeout_seconds=float(ms.activity_timeout_seconds),
                                 max_read_calls=ms.builder_max_read_calls, submit_check=h.submit_check(state))
    scored = score(str(ws.path), task)
    return {"id": task["id"], "result": classify(res, codes, scored), "submitted": res.ok, "error_class": res.error_class,
            "turns": res.turns, "test_runs": state.test_runs, "patches": state.patches_applied, "cost_usd": str(res.cost_usd),
            "tokens_in": res.tokens_in, "tokens_out": res.tokens_out, "tool_codes": codes[-10:]}


def summarize(rows: List[Dict[str, Any]], *, live: bool, model_name: str, config: Dict[str, Any], skipped: List[str]) -> Dict[str, Any]:
    ran = [r for r in rows if r["result"] != "setup_error"]
    n = len(ran) or 1
    classes: Dict[str, int] = {}
    for r in ran:
        classes[r["result"]] = classes.get(r["result"], 0) + 1
    return {
        "live": live, "model": model_name, "config": config,
        "tasks_run": len(ran), "tasks_not_run_budget": skipped, "setup_errors": [r["id"] for r in rows if r["result"] == "setup_error"], "passed": classes.get("pass", 0),
        "pass_at_1": round(classes.get("pass", 0) / n, 3),
        "mean_turns": round(sum(r["turns"] for r in ran) / n, 2), "mean_test_runs": round(sum(r["test_runs"] for r in ran) / n, 2),
        "cost_usd": str(sum((Decimal(r["cost_usd"]) for r in ran), Decimal("0"))), "result_classes": classes,
    }


def run_config(ms: MaintenanceSettings, ss: SocietySettings) -> Dict[str, Any]:
    return {"builder_max_turns": ms.builder_max_turns, "builder_max_read_calls": ms.builder_max_read_calls, "max_test_runs": ms.max_test_runs_per_attempt,
            "activity_timeout_s": ms.activity_timeout_seconds, "model_timeout_s": ss.model_timeout_seconds, "thinking": ss.model_thinking_mode,
            "max_output_tokens": ss.model_max_output_tokens}


async def bench(tasks: List[dict], *, repo: str, model, budget: Decimal, ms: Optional[MaintenanceSettings] = None, ss: Optional[SocietySettings] = None,
                emit=lambda line: print(line, flush=True)) -> Dict[str, Any]:
    ms, ss = ms or MaintenanceSettings(), ss or SocietySettings()
    rows: List[Dict[str, Any]] = []
    skipped: List[str] = []
    spent = Decimal("0")
    with tempfile.TemporaryDirectory(prefix="agentnet-bench-") as root:
        for task in tasks:
            if spent >= budget:
                skipped.append(task["id"])
                continue
            try:
                row = await run_task(task, repo=repo, root=root, model=model, ms=ms, ss=ss, cost_cap=budget - spent)
            except subprocess.CalledProcessError as exc:  # the task could not be set up: not a model result
                row = {"id": task["id"], "result": "setup_error", "detail": " ".join(map(str, exc.cmd))[:200], "turns": 0, "test_runs": 0, "cost_usd": "0"}
            spent += Decimal(row["cost_usd"])
            rows.append(row)
            emit("BENCH TASK " + json.dumps(row, sort_keys=True))
        subprocess.run(["git", "worktree", "prune"], cwd=repo, check=False, capture_output=True)
    summary = summarize(rows, live=bool(getattr(model, "live", False)), model_name=getattr(model, "model_name", "?"), config=run_config(ms, ss), skipped=skipped)
    emit("BENCH SUMMARY " + json.dumps(summary, sort_keys=True))
    return summary


def load_tasks(only: str = "") -> List[dict]:
    tasks = json.loads(TASKS.read_text(encoding="utf-8"))["tasks"]
    wanted = {t for t in only.split(",") if t}
    return [t for t in tasks if not wanted or t["id"] in wanted]


def main(argv=None, *, model=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=str(ROOT))
    ap.add_argument("--only", default="")
    ap.add_argument("--json-out")
    ap.add_argument("--allow-scripted", action="store_true", help="bench self-tests only: the report says live=false")
    args = ap.parse_args(argv)
    for k in [k for k in os.environ if k.startswith(("POSTGRES", "REDIS", "DATABASE_URL"))]:
        os.environ.pop(k)  # the bench never touches a database, and neither do the tests it runs
    model = model or act.get_activity_model()
    if model is None or (not getattr(model, "live", False) and not args.allow_scripted):
        print("BENCH REFUSED: no live model configured (SOCIETY_MODEL_PROVIDER=openai_compatible + key + base URL); scripted output is never a bench result")
        return 2
    summary = asyncio.run(bench(load_tasks(args.only), repo=args.repo, model=model, budget=Decimal(os.getenv("BENCH_BUDGET_USD", "1"))))
    if args.json_out:
        pathlib.Path(args.json_out).write_text(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
