"""Offline builder bench: how often does the maintenance builder loop fix a real task?

For each task in tasks.json: a worktree at ``fix_sha`` with ``files_allowed``
reverted to ``fix_sha^`` (the task's tests present, its fix absent). The
maintenance AuthorPatch loop (harness.builder_tools + activities.run_activity,
exactly as the kernel runs it) works the task with the configured model;
scripts/bench/score.py then judges the worktree.

    python scripts/bench/run.py --repo <checkout with the task commits> [--only id,id] [--repeat 3] [--path maintenance|society] [--json-out f]

``--path maintenance`` (default) feeds the task as a repair plan, as the
maintenance kernel does; ``--path society`` feeds it as a Society candidate
spec through ``society/engineering/build_engine.build`` -- the function the
Society Builder's BUILD_CODE_CANDIDATE runs. Both drive the same harness.

Each task runs ``--repeat`` times (default 3). A run is *delivered* only when
the model SUBMITTED and the judge scored the worktree: a correct worktree left
behind by a run that hit its turn budget is not a delivery. pass@1 is the
delivered rate over all runs; pass@k counts a task once any of its k runs
delivered. One run is best-of-``MAINTENANCE_BUILDER_SAMPLES`` (harness.author_patch):
samples stop at the first one the harness's own acceptance gate delivers --
never picked by this judge.

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

from scripts.bench.score import score  # noqa: E402  -- the judge is ALWAYS this revision's

# BENCH_HARNESS_ROOT: score a CANDIDATE harness (another checkout's services/)
# with this revision's judge and tasks -- imported above, before the switch.
HARNESS_ROOT = pathlib.Path(os.environ.get("BENCH_HARNESS_ROOT") or ROOT).resolve()
if HARNESS_ROOT != ROOT:
    sys.path.insert(0, str(HARNESS_ROOT))
from services.registry.app.maintenance import activities as act  # noqa: E402
from services.registry.app.maintenance import harness as h  # noqa: E402
from services.registry.app.maintenance.config import MaintenanceSettings  # noqa: E402
from services.registry.app.society.config import SocietySettings  # noqa: E402
from services.registry.app.society.engineering import build_engine  # noqa: E402
from services.registry.app.society.engineering import workspace as ws_mod  # noqa: E402

TASKS = pathlib.Path(__file__).with_name("tasks.json")
HOLDOUT = pathlib.Path(__file__).with_name("holdout.json")
SPLITS = ("dev", "holdout", "all")
PATCH_PROTOCOL = build_engine.PATCH_PROTOCOL
PATHS = ("maintenance", "society")
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
        "target_files": {"trust": "untrusted_repository_data", "files": h.target_file_context(ws.path, task["files_allowed"], [task["task"]], tests=task["failing_tests"])},
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
    if scored["passed"] and res.ok:
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


def society_spec(task: dict) -> Dict[str, Any]:
    """The task as the Architect would hand it to the Society Builder."""
    return {"kind": "code", "description": task["task"], "files_allowed": list(task["files_allowed"]), "acceptance_tests": list(task["failing_tests"])}


async def run_task(task: dict, *, repo: str, root: str, rep: int = 0, model, ms: MaintenanceSettings, ss: SocietySettings, cost_cap: Decimal,
                   path: str = "maintenance") -> Dict[str, Any]:
    ws = prepare(repo, task, os.path.join(root, f"r{rep}"))
    codes: List[str] = []
    if path == "society":
        out = await build_engine.build(ws, society_spec(task), title=task["id"], model=model, ms=ms, ss=ss, cost_cap=cost_cap,
                                       wrap_tools=lambda tools: _recording(tools, codes))
        res, state, samples = out.result, out.state, out.samples
    else:  # as the reconciler does
        run = await h.author_patch(ws, task_input(task, ws, ms), files_allowed=task["files_allowed"], tests=task["failing_tests"], model=model, settings=ms,
                                   test_timeout=ss.qa_test_timeout_seconds, cost_cap=cost_cap, wrap_tools=lambda tools: _recording(tools, codes))
        res, state, samples = run.result, run.state, run.samples
    scored = score(str(ws.path), task)
    return {"id": task["id"], "split": task.get("split", "dev"), "rep": rep, "result": classify(res, codes, scored), "submitted": res.ok, "scored": scored["passed"],
            "auto_submitted": bool((res.output or {}).get("auto_submitted")), "tests_unverified": bool((res.output or {}).get("tests_unverified")), "error_class": res.error_class,
            "turns": res.turns, "test_runs": state.test_runs, "samples": len(samples), "sample_results": [s["error_class"] or ("pass" if s["delivered"] else "rescope") for s in samples], "patches": state.patches_applied, "cost_usd": str(res.cost_usd),
            "tokens_in": res.tokens_in, "tokens_out": res.tokens_out, "tool_codes": codes[-10:],
            "actions": [str(t.get("refused") and f"submit!{t['refused']}" or t.get("action")) for t in res.turn_log]}


def summarize(rows: List[Dict[str, Any]], *, live: bool, model_name: str, config: Dict[str, Any], skipped: List[str]) -> Dict[str, Any]:
    ran = [r for r in rows if r["result"] != "setup_error"]
    splits = {r["id"]: r.get("split", "dev") for r in rows}
    n = len(ran) or 1
    classes: Dict[str, int] = {}
    matrix: Dict[str, List[str]] = {}
    for r in ran:
        classes[r["result"]] = classes.get(r["result"], 0) + 1
        matrix.setdefault(r["id"], []).append(r["result"])
    delivered = classes.get("pass", 0)
    return {
        "live": live, "model": model_name, "config": config, "repeat": config.get("repeat", 1),
        "tasks_run": len(matrix), "runs": len(ran), "tasks_not_run_budget": sorted(set(skipped)),
        "setup_errors": sorted({r["id"] for r in rows if r["result"] == "setup_error"}),
        "delivered": delivered, "pass_at_1": round(delivered / n, 3),
        "pass_at_k": round(sum("pass" in v for v in matrix.values()) / (len(matrix) or 1), 3),
        "undelivered_correct": sum(1 for r in ran if r.get("scored") and r["result"] != "pass"),
        "mean_turns": round(sum(r["turns"] for r in ran) / n, 2), "mean_test_runs": round(sum(r["test_runs"] for r in ran) / n, 2),
        "cost_usd": str(sum((Decimal(r["cost_usd"]) for r in ran), Decimal("0"))),
        "cost_per_task_usd": str(round(sum((Decimal(r["cost_usd"]) for r in ran), Decimal("0")) / n, 4)),
        "result_classes": classes, "per_task": {k: {"delivered": v.count("pass"), "runs": v, "split": splits.get(k, "dev")} for k, v in sorted(matrix.items())},
        "splits": {sp: _split_score(sp, matrix, splits) for sp in sorted(set(splits.values()))},
    }


def _split_score(split: str, matrix: Dict[str, List[str]], splits: Dict[str, str]) -> Dict[str, Any]:
    runs = {k: v for k, v in matrix.items() if splits.get(k, "dev") == split}
    n = sum(len(v) for v in runs.values()) or 1
    return {"tasks": len(runs), "pass_at_1": round(sum(v.count("pass") for v in runs.values()) / n, 3),
            "pass_at_k": round(sum("pass" in v for v in runs.values()) / (len(runs) or 1), 3)}


def harness_verdict(baseline: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Merge rule for a harness change (both reports judged by the RUNNING
    revision): holdout delivered pass@1 must improve and no task may fall
    from 3/3 to 0/3. Deterministic; the owner still merges."""
    b, c = baseline.get("per_task") or {}, candidate.get("per_task") or {}
    regressed = sorted(k for k, v in b.items() if v["delivered"] == len(v["runs"]) > 0 and k in c and c[k]["delivered"] == 0)
    hb, hc = (r.get("splits", {}).get("holdout", {}).get("pass_at_1") for r in (baseline, candidate))
    improves = hb is not None and hc is not None and hc > hb
    return {"holdout_pass_at_1": {"baseline": hb, "candidate": hc}, "holdout_improves": improves, "regressed_3_to_0": regressed,
            "dev_pass_at_1": {"baseline": baseline.get("splits", {}).get("dev", {}).get("pass_at_1"), "candidate": candidate.get("splits", {}).get("dev", {}).get("pass_at_1")},
            "mergeable": improves and not regressed}


def report_row(summary: Dict[str, Any], *, revision: str, judge_revision: str) -> Dict[str, Any]:
    """The society_bench_reports row: aggregates only (no task text, no model output)."""
    keys = ("pass_at_1", "pass_at_k", "delivered", "runs", "tasks_run", "cost_usd", "cost_per_task_usd", "live", "splits", "result_classes")
    return {"revision": revision, "judge_revision": judge_revision, "path": summary["config"].get("path", "maintenance"), "model": summary.get("model"),
            "repeat": int(summary.get("repeat") or 1), "summary": {k: summary.get(k) for k in keys},
            "per_task": {k: {"split": v["split"], "delivered": v["delivered"], "runs": v["runs"]} for k, v in summary["per_task"].items()}}


def run_config(ms: MaintenanceSettings, ss: SocietySettings, repeat: int = 1, path: str = "maintenance") -> Dict[str, Any]:
    # MAINTENANCE_BUILDER_MAX_OUTPUT_TOKENS is what each AuthorPatch model turn
    # actually gets; SOCIETY_MODEL_MAX_OUTPUT_TOKENS does not reach it. A run is
    # best-of-builder_samples, delivered by the harness's own gate.
    return {"builder_max_turns": ms.builder_max_turns, "builder_max_read_calls": ms.builder_max_read_calls, "max_test_runs": ms.max_test_runs_per_attempt,
            "activity_timeout_s": ms.activity_timeout_seconds, "model_timeout_s": ss.model_timeout_seconds, "thinking": ss.model_thinking_mode,
            "effective_max_tokens_per_turn": ms.builder_max_output_tokens, "builder_samples": ms.builder_samples,
            "sample_temperatures": list(h.SAMPLE_TEMPERATURES[:ms.builder_samples]), "repeat": repeat, "path": path}


async def bench(tasks: List[dict], *, repo: str, model, budget: Decimal, ms: Optional[MaintenanceSettings] = None, ss: Optional[SocietySettings] = None,
                repeat: int = 1, path: str = "maintenance", emit=lambda line: print(line, flush=True)) -> Dict[str, Any]:
    ms, ss = ms or MaintenanceSettings(), ss or SocietySettings()
    rows: List[Dict[str, Any]] = []
    skipped: List[str] = []
    spent = Decimal("0")
    with tempfile.TemporaryDirectory(prefix="agentnet-bench-") as root:
        for rep, task in [(rep, task) for rep in range(repeat) for task in tasks]:
            if spent >= budget:
                skipped.append(task["id"])
                continue
            try:
                row = await run_task(task, repo=repo, root=root, rep=rep, model=model, ms=ms, ss=ss, cost_cap=budget - spent, path=path)
            except subprocess.CalledProcessError as exc:  # the task could not be set up: not a model result
                row = {"id": task["id"], "split": task.get("split", "dev"), "rep": rep, "result": "setup_error", "detail": " ".join(map(str, exc.cmd))[:200], "turns": 0, "test_runs": 0, "cost_usd": "0"}
            spent += Decimal(row["cost_usd"])
            rows.append(row)
            emit("BENCH TASK " + json.dumps(row, sort_keys=True))
        subprocess.run(["git", "worktree", "prune"], cwd=repo, check=False, capture_output=True)
    summary = summarize(rows, live=bool(getattr(model, "live", False)), model_name=getattr(model, "model_name", "?"), config=run_config(ms, ss, repeat, path), skipped=skipped)
    emit("BENCH SUMMARY " + json.dumps(summary, sort_keys=True))
    return summary


def load_tasks(only: str = "", split: str = "dev") -> List[dict]:
    """``split``: dev (tasks.json), holdout (holdout.json) or all; each task is tagged with its split."""
    tasks = [{**t, "split": "dev"} for t in json.loads(TASKS.read_text(encoding="utf-8"))["tasks"]] if split in ("dev", "all") else []
    if split in ("holdout", "all") and HOLDOUT.exists():
        tasks += [{**t, "split": "holdout"} for t in json.loads(HOLDOUT.read_text(encoding="utf-8"))["tasks"]]
    wanted = {t for t in only.split(",") if t}
    return [t for t in tasks if not wanted or t["id"] in wanted]


def main(argv=None, *, model=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=str(ROOT))
    ap.add_argument("--only", default="")
    ap.add_argument("--repeat", type=int, default=3, help="runs per task (pass@k over these)")
    ap.add_argument("--path", choices=PATHS, default="maintenance", help="which builder entry point drives the harness")
    ap.add_argument("--split", choices=SPLITS, default="all", help="dev (Society-visible), holdout (controller-only) or all")
    ap.add_argument("--json-out")
    ap.add_argument("--allow-scripted", action="store_true", help="bench self-tests only: the report says live=false")
    args = ap.parse_args(argv)
    model = model or act.get_activity_model()
    if model is None or (not getattr(model, "live", False) and not args.allow_scripted):
        print("BENCH REFUSED: no live model configured (SOCIETY_MODEL_PROVIDER=openai_compatible + key + base URL); scripted output is never a bench result")
        return 2
    for k in [k for k in os.environ if k.startswith(("POSTGRES", "REDIS", "DATABASE_URL"))]:
        os.environ.pop(k)  # the bench never touches a database, and neither do the tests it runs
    summary = asyncio.run(bench(load_tasks(args.only, args.split), repo=args.repo, model=model, budget=Decimal(os.getenv("BENCH_BUDGET_USD", "1")),
                                repeat=max(1, args.repeat), path=args.path))
    if args.json_out:
        pathlib.Path(args.json_out).write_text(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
