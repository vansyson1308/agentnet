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
import importlib.metadata
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import uuid
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

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
# Holdout tasks from OTHER repositories (a pinned SWE-bench Verified subset):
# base commit + the task's test patch is the attempt's base; never readable by a model tool.
HOLDOUT_EXTERNAL = pathlib.Path(__file__).with_name("holdout_external.json")
EXTERNAL_CACHE = pathlib.Path(os.environ.get("BENCH_EXTERNAL_CACHE") or pathlib.Path(tempfile.gettempdir()) / "agentnet-bench-ext")
SPLITS = ("dev", "holdout", "all")
PATCH_PROTOCOL = build_engine.PATCH_PROTOCOL
PATHS = ("maintenance", "society")
WRONG_FILE = ("out_of_scope", "protected", "path")


def _git(args: List[str], cwd: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def external_checkout(task: dict) -> str:
    """A blobless clone of the task's repository (cached), holding its base commit."""
    dest = EXTERNAL_CACHE / re.sub(r"[^A-Za-z0-9]+", "_", task["repo"].split("github.com/")[-1])
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout", task["repo"], str(dest)], check=True, capture_output=True, timeout=1800)
    if subprocess.run(["git", "-C", str(dest), "cat-file", "-e", task["base_sha"] + "^{commit}"], capture_output=True).returncode:
        _git(["fetch", "-q", "origin", task["base_sha"]], str(dest))
    return str(dest)


def ensure_deps(tasks: List[dict]) -> None:
    """pip-install the pinned packages external tasks need (``name==version``), once."""
    def have(spec: str) -> bool:
        name, _, ver = spec.partition("==")
        try:
            return importlib.metadata.version(name) == ver
        except importlib.metadata.PackageNotFoundError:
            return False
    missing = sorted({d for t in tasks for d in t.get("deps", []) if not have(d)})
    if missing:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", *missing], check=True, capture_output=True, timeout=900)


def prepare(repo: str, task: dict, root: str) -> ws_mod.Workspace:
    """Worktree at fix_sha with the fix reverted (or, for an external task, at its
    base commit with its test patch applied), committed as the attempt's base."""
    path = os.path.join(root, task["id"])
    if task.get("repo"):
        repo = external_checkout(task)
        _git(["worktree", "add", "-q", "--detach", path, task["base_sha"]], repo)
        subprocess.run(["git", "apply", "-"], cwd=path, input=task["test_patch"], text=True, check=True, capture_output=True)
        _git(["add", "-A"], path)
    else:
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


#: Per-run structural fields a ticket's work packet reads (codes and counts; never model text).
DETAIL_KEYS = ("result", "error_class", "turns", "test_runs", "patches", "samples", "sample_results", "tool_codes")


def run_detail(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: row.get(k) for k in DETAIL_KEYS if k in row}


def summarize(rows: List[Dict[str, Any]], *, live: bool, model_name: str, config: Dict[str, Any], skipped: List[str]) -> Dict[str, Any]:
    ran = [r for r in rows if r["result"] != "setup_error"]
    splits = {r["id"]: r.get("split", "dev") for r in rows}
    n = len(ran) or 1
    classes: Dict[str, int] = {}
    matrix: Dict[str, List[str]] = {}
    details: Dict[str, List[Dict[str, Any]]] = {}
    for r in ran:
        classes[r["result"]] = classes.get(r["result"], 0) + 1
        matrix.setdefault(r["id"], []).append(r["result"])
        details.setdefault(r["id"], []).append(run_detail(r))
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
        "result_classes": classes, "per_task": {k: {"delivered": v.count("pass"), "runs": v, "split": splits.get(k, "dev"), "detail": details.get(k, [])} for k, v in sorted(matrix.items())},
        "splits": {sp: _split_score(sp, matrix, splits) for sp in sorted(set(splits.values()))},
    }


def _split_score(split: str, matrix: Dict[str, List[str]], splits: Dict[str, str]) -> Dict[str, Any]:
    runs = {k: v for k, v in matrix.items() if splits.get(k, "dev") == split}
    n = sum(len(v) for v in runs.values()) or 1
    return {"tasks": len(runs), "pass_at_1": round(sum(v.count("pass") for v in runs.values()) / n, 3),
            "pass_at_k": round(sum("pass" in v for v in runs.values()) / (len(runs) or 1), 3)}


def _units(report: Dict[str, Any], split: str, skip: frozenset = frozenset()) -> float:
    """Delivered tasks of a split: sum of delivered/runs per task (a 2/3 task counts 0.67)."""
    return sum(v["delivered"] / len(v["runs"]) for k, v in (report.get("per_task") or {}).items() if v["split"] == split and v["runs"] and k not in skip)


def _cost_per_delivered(report: Dict[str, Any]) -> Optional[float]:
    return float(report["cost_usd"]) / report["delivered"] if report.get("delivered") else None


def partition(reports: List[Dict[str, Any]], *, min_samples: int = 3) -> Dict[str, List[str]]:
    """Regression set: tasks the running revision delivered 3/3 with best-of-``min_samples``
    on each of its two latest such reports (latest first). They still must not regress;
    every other task is ACTIVE and feeds the backlog and the verdict."""
    best = [r for r in reports if int((r.get("config") or {}).get("builder_samples") or 1) >= min_samples][:2]
    if len(best) < 2:
        return {"regression": [], "reports": len(best)}
    full = [{k for k, v in (r.get("per_task") or {}).items() if len(v["runs"]) >= 3 and v["delivered"] == len(v["runs"])} for r in best]
    return {"regression": sorted(full[0] & full[1]), "reports": 2}


def harness_verdict(baseline: Dict[str, Any], candidate: Dict[str, Any], regression: Iterable[str] = ()) -> Dict[str, Any]:
    """Merge rule for a harness change (both reports judged by the RUNNING revision),
    over the ACTIVE tasks: holdout delivered not worse by more than 1 task AND (dev or
    holdout improves by >= 2 tasks OR cost per delivered task drops >= 15%) AND no task
    falls 3/3 -> 0/3 AND no regression-set task loses more than one run. A baseline whose
    active holdout pass@1 >= 0.9 cannot tell: ``bench_saturated``. The owner still merges."""
    reg = frozenset(regression)
    b, c = baseline.get("per_task") or {}, candidate.get("per_task") or {}
    fell = sorted(k for k, v in b.items() if v["delivered"] == len(v["runs"]) > 0 and k in c and c[k]["delivered"] == 0)
    reg_fell = sorted(k for k in reg if k in b and k in c and c[k]["delivered"] < b[k]["delivered"] - 1)
    hb, hc, db_, dc = _units(baseline, "holdout", reg), _units(candidate, "holdout", reg), _units(baseline, "dev", reg), _units(candidate, "dev", reg)
    runs = sum(len(v["runs"]) for k, v in b.items() if v["split"] == "holdout" and k not in reg)
    hold_rate = round(sum(v["delivered"] for k, v in b.items() if v["split"] == "holdout" and k not in reg) / runs, 3) if runs else None
    cb, cc = _cost_per_delivered(baseline), _cost_per_delivered(candidate)
    cheaper = cb is not None and cc is not None and cc <= 0.85 * cb
    improves = dc - db_ >= 2 or hc - hb >= 2
    ok = hc >= hb - 1 and (improves or cheaper) and not fell and not reg_fell
    verdict = "bench_saturated" if hold_rate is None or hold_rate >= 0.9 else ("mergeable" if ok else "not_mergeable")
    return {"verdict": verdict, "mergeable": verdict == "mergeable", "active_holdout_pass_at_1_baseline": hold_rate,
            "holdout_tasks": {"baseline": round(hb, 2), "candidate": round(hc, 2)}, "dev_tasks": {"baseline": round(db_, 2), "candidate": round(dc, 2)},
            "cost_per_delivered_usd": {"baseline": cb and round(cb, 4), "candidate": cc and round(cc, 4)}, "regressed_3_to_0": fell, "regression_set_fell": reg_fell,
            "regression_set": sorted(reg), "note": "bench saturated: add harder tasks" if verdict == "bench_saturated" else ""}


def report_row(summary: Dict[str, Any], *, revision: str, judge_revision: str) -> Dict[str, Any]:
    """The society_bench_reports row: aggregates only (no task text, no model output)."""
    keys = ("pass_at_1", "pass_at_k", "delivered", "runs", "tasks_run", "cost_usd", "cost_per_task_usd", "live", "splits", "result_classes", "config")
    return {"revision": revision, "judge_revision": judge_revision, "path": summary["config"].get("path", "maintenance"), "model": summary.get("model"),
            "repeat": int(summary.get("repeat") or 1), "summary": {k: summary.get(k) for k in keys},
            "per_task": {k: {"split": v["split"], "delivered": v["delivered"], "runs": v["runs"],
                             # dev only: the structural failure evidence a ticket's work packet carries (holdout stays scores)
                             **({"detail": v.get("detail") or []} if v["split"] == "dev" else {})} for k, v in summary["per_task"].items()}}


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
    if split in ("holdout", "all"):
        tasks += [{**t, "split": "holdout"} for f in (HOLDOUT, HOLDOUT_EXTERNAL) if f.exists() for t in json.loads(f.read_text(encoding="utf-8"))["tasks"]]
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
    tasks = load_tasks(args.only, args.split)
    ensure_deps(tasks)
    summary = asyncio.run(bench(tasks, repo=args.repo, model=model, budget=Decimal(os.getenv("BENCH_BUDGET_USD", "1")),
                                repeat=max(1, args.repeat), path=args.path))
    if args.json_out:
        pathlib.Path(args.json_out).write_text(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
