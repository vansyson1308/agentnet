"""A ticket's work packet: what to change, from structural evidence (no model).

A bench ticket ("Builder harness: deliver dev task X (k/3 runs)") names WHAT
failed, not WHAT to change. The work packet turns it into a design input:

* the failure evidence of the latest bench report that ran the task: the
  result class of every run and, where the report carries it (scripts/bench
  ``run_detail``), turns, test runs, patches and the last tool codes;
* the target: the builder-harness file and function the dominant failure
  class implicates (``TARGETS``; a last tool code refines it);
* the proof: ``bench:<task>`` -- QA re-runs that task x3 with the existing
  bench runner on the CANDIDATE harness (``engineering/bench_proof.py``),
  judged by the running revision. Never a pytest node id;
* the regression tests the harness change must keep green -- minus any test file
  the baseline gate found red on the untouched base in the last
  ``RED_ON_BASE_DAYS`` (no patch can turn those green, and QA would fail them).

Only dev tasks have a packet: holdout tasks never reach a ticket or a context.
Task text comes from ``scripts/bench/tasks.json`` (dev, Society-visible) and is
marked as such; nothing here is model output.
"""

from __future__ import annotations

import json
import pathlib
from collections import Counter
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .backlog import HARNESS_PATHS

ACTIVITIES = "services/registry/app/maintenance/activities.py"
HARNESS = "services/registry/app/maintenance/harness.py"
REPO_TOOLS = "services/registry/app/maintenance/repo_tools.py"
#: dominant failure class (scripts/bench/run.py ``classify``) -> (file, function, why)
TARGETS: Dict[str, tuple] = {
    "turn_budget": (ACTIVITIES, "run_activity", "the builder ran out of turns before a green submit: turn budgeting, the final-turn directive, read-vs-edit pacing"),
    "timeout": (HARNESS, "author_patch", "a try exceeded its time budget: per-sample time, test timeout, when sampling stops"),
    "truncated_output": (ACTIVITIES, "_parse", "replies were cut at the token limit: patch size, the patch protocol, smaller steps"),
    "syntax": (HARNESS, "submit_check", "submitted edits did not compile: the syntax gate and how its error reaches the model"),
    "wrong_file": (HARNESS, "target_file_context", "the builder edited or asked for files outside the plan: which files and spans the context shows"),
    "tests_fail": (HARNESS, "submit_check", "the builder submitted with failing acceptance tests: how test failures are shown and when submit is refused"),
    "cost_budget": (HARNESS, "author_patch", "a try hit its cost cap: samples x turns x tokens"),
}
DEFAULT_TARGET = (ACTIVITIES, "run_activity", "the try ended without a valid answer: the tool loop and its corrective turn")
#: last tool code -> target (more specific than the class)
CODE_TARGETS: Dict[str, tuple] = {
    "read_budget": (ACTIVITIES, "run_activity", "the read budget was used up before an edit: read pacing and the read-budget directive"),
    "invalid_patch": (HARNESS, "builder_tools", "patches did not apply: the exact-text patch tool and its nearest-match feedback (_nearest)"),
    "tests_failing": (HARNESS, "run_targeted_tests", "targeted tests kept failing: the failure summary the builder sees"),
    "syntax_error": (HARNESS, "submit_check", "edits did not compile: the syntax gate feedback"),
    "empty_patch": (HARNESS, "submit_check", "submits carried no change: the empty/format-only refusal"),
    "format_only": (HARNESS, "submit_check", "submits changed whitespace only: the format-only refusal"),
    "out_of_scope": (HARNESS, "target_file_context", "edits targeted files outside the plan"),
}
#: tests the harness change keeps green (QA runs them next to the bench proof)
REGRESSION_TESTS = ("tests/society/maintenance/test_author_patch_loop.py", "tests/society/maintenance/test_activity_loop.py",
                    "tests/society/maintenance/test_builder_context.py", "tests/test_bench.py")
#: how long a baseline-gate verdict (executor._red_on_base) keeps a test file out of bench specs
RED_ON_BASE_DAYS = 7
BENCH_PROOF_RULE = "QA re-runs the task x3 with scripts/bench on the candidate harness (judged by the running revision): delivered >= 2/3 and more than the baseline"
TASKS_REL = "scripts/bench/tasks.json"


def default_tasks_file(module: pathlib.Path = pathlib.Path(__file__)) -> pathlib.Path:
    """tasks.json of the checkout this module runs from. The registry image has no
    repository around /app/app/society/ (no scripts/): then a path that does not exist,
    and dev_task() returns None -- never an import-time crash."""
    parents = module.resolve().parents
    return (parents[4] if len(parents) > 4 else parents[-1]) / TASKS_REL


_TASKS = default_tasks_file()


def bench_task(ticket: Optional[Dict[str, Any]]) -> Optional[str]:
    for p in (ticket or {}).get("proof") or []:
        if isinstance(p, str) and p.startswith("bench:"):
            return p.split(":", 1)[1]
    return None


def dev_task(task_id: str, tasks_file: pathlib.Path = _TASKS) -> Optional[Dict[str, Any]]:
    """A dev bench task (tasks.json) or None -- holdout files are never read here."""
    try:
        tasks = json.loads(tasks_file.read_text(encoding="utf-8"))["tasks"]
    except (OSError, ValueError, KeyError):
        return None
    return next((t for t in tasks if t.get("id") == task_id), None)


def latest_task_report(db: Session, task_id: str, revision: str = "") -> Optional[Dict[str, Any]]:
    """The newest report in which main's own harness ran this task (revision == judge);
    one of ``revision`` (the running one) first when it exists."""
    row = db.execute(text("SELECT id, revision, created_at, per_task -> :t AS v FROM society_bench_reports "
                          "WHERE revision = judge_revision AND per_task ? :t ORDER BY (revision = :r) DESC, created_at DESC LIMIT 1"),
                     {"t": task_id, "r": revision}).mappings().first()
    return dict(row) if row else None


def red_on_base_files(db: Session, days: int = RED_ON_BASE_DAYS) -> List[str]:
    """Test files the baseline gate found red on the untouched base recently (its
    CODE_CANDIDATE_REJECTED events): staging 2026-10-13, every harness spec named
    tests/test_bench.py and every one was rejected for it."""
    rows = db.execute(text("SELECT payload -> 'red_on_base' FROM society_events WHERE event_type = 'code_candidate.rejected' "
                           "AND payload ? 'red_on_base' AND created_at > now() - make_interval(days => :d)"), {"d": days}).scalars().all()
    return sorted({str(t).split("::")[0] for ids in rows if isinstance(ids, list) for t in ids})


def regression_tests(db: Session) -> List[str]:
    red = set(red_on_base_files(db))
    return [t for t in REGRESSION_TESTS if t not in red]


def target_for(failure_class: Optional[str], last_code: Optional[str]) -> Dict[str, str]:
    f, fn, why = CODE_TARGETS.get(last_code or "") or TARGETS.get(failure_class or "") or DEFAULT_TARGET
    return {"file": f, "function": fn, "why": why}


def bench_packet(db: Session, task_id: str, repo_root: str = "") -> Optional[Dict[str, Any]]:
    report = latest_task_report(db, task_id)
    v = (report or {}).get("v") or {}
    if v.get("split") != "dev":
        return None  # holdout, or no report ran it
    runs: List[str] = [str(r) for r in v.get("runs") or []]
    detail: List[Dict[str, Any]] = [d for d in v.get("detail") or [] if isinstance(d, dict)]
    fails = [r for r in runs if r != "pass"]
    dominant = Counter(fails).most_common(1)[0][0] if fails else None
    last_codes = [str((d.get("tool_codes") or [None])[-1]) for d in detail if d.get("result") != "pass" and d.get("tool_codes")]
    code = Counter(last_codes).most_common(1)[0][0] if last_codes else None
    task = dev_task(task_id, pathlib.Path(repo_root) / TASKS_REL if repo_root else _TASKS) or {}
    return {
        "kind": "bench",
        "task_id": task_id,
        "change": ("change the BUILDER HARNESS so tasks like this one are delivered; the task itself is already solved on main -- "
                   "never edit the task's own files"),
        "task": {"trust": "bench_task", "text": str(task.get("task") or "")[:600], "files": list(task.get("files_allowed") or [])[:8],
                 "failing_tests": list(task.get("failing_tests") or [])[:8]},
        "evidence": {"report": str(report["id"])[:8], "revision": str(report["revision"])[:12],
                     "at": report["created_at"].isoformat() if report.get("created_at") else None,
                     "delivered": f"{int(v.get('delivered') or 0)}/{len(runs)}", "result_classes": runs,
                     "runs": [{k: d.get(k) for k in ("result", "error_class", "turns", "test_runs", "patches", "sample_results")} | {"last_tool_codes": list(d.get("tool_codes") or [])[-5:]}
                              for d in detail][:6],
                     "failure_class": dominant, "last_tool_code": code},
        "target": target_for(dominant, code),
        "files_allowed": {"harness": list(HARNESS_PATHS), "rule": "at least one harness file; anything else only under tests/"},
        "proof": {"acceptance": f"bench:{task_id}", "rule": BENCH_PROOF_RULE, "baseline_delivered": int(v.get("delivered") or 0), "repeat": 3},
        "regression_tests": regression_tests(db),
        "red_on_base": red_on_base_files(db),
    }


#: Where a growth key result is moved (backlog.kr_gap_items tickets): the product surfaces a
#: real user or agent passes through. A starting point for the design, not an allow-list.
METRIC_LEVERS: Dict[str, Dict[str, Any]] = {
    "a2a_agents_live_card": {
        "means": "remote A2A agents whose card AgentNet validated in the last 7 days (the federation catalog)",
        "levers": ["services/dashboard/app/templates/network.html", "services/dashboard/app/templates/network_agent.html",
                   "services/dashboard/app/templates/directory.html", "docs/A2A_QUICKSTART.md", "docs/A2A_FEDERATION.md",
                   "examples/reference_agent.py", "sdk/python/agentnet/client.py"],
        "ideas": ["make publishing and registering an A2A agent card a one-page, copy-paste flow",
                  "show federated agents and their live status where visitors look first",
                  "a reference agent another developer can deploy and register in minutes"],
    },
    "escrow_tasks_completed_7d": {
        "means": "completed escrow tasks between two distinct agents in the last 7 days (the marketplace)",
        "levers": ["services/dashboard/app/templates/marketplace.html", "services/dashboard/app/templates/agent_register.html",
                   "services/dashboard/app/templates/new_agent.html", "services/dashboard/app/templates/create_offer.html",
                   "docs/AGENT_DEVELOPMENT.md", "examples/agent_sdk.py", "sdk/python/agentnet/client.py"],
        "ideas": ["shorten the path from sign-up to a first paid task", "make offers and their prices visible to callers",
                  "a working example of one agent hiring another through escrow"],
    },
}


def lever_packet(ticket: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The packet of a growth ticket (proof probe:<metric>): what the metric means, where it moves,
    and how it is judged (outcomes.py reads the KR at merge, +24h and +7d)."""
    proof = [p for p in ticket.get("proof") or [] if isinstance(p, str) and p.startswith("probe:")]
    metric = proof[0].split(":", 1)[1] if proof else None
    lever = METRIC_LEVERS.get(metric or "")
    if lever is None:
        return None
    return {"kind": "growth", "metric_id": metric, **lever,
            "proof": {"rule": "the key result is read at merge, +24h and +7d (outcomes.py): moved, no_effect or regressed",
                      "acceptance": "name pytest tests for the behaviour you change; the probe is not a pytest target"}}


def for_ticket(db: Session, ticket: Optional[Dict[str, Any]], repo_root: str = "") -> Optional[Dict[str, Any]]:
    task_id = bench_task(ticket)
    if task_id:
        return bench_packet(db, task_id, repo_root)
    return lever_packet(ticket) if ticket else None


def harness_scope_refusal(files: List[str]) -> Optional[str]:
    """A bench ticket changes the builder harness: >= 1 harness file, the rest only tests."""
    if not any(f in HARNESS_PATHS for f in files):
        return f"a bench ticket changes the builder harness: files_allowed must include one of {list(HARNESS_PATHS)}"
    other = [f for f in files if f not in HARNESS_PATHS and not f.startswith("tests/")]
    if other:
        return f"a bench ticket changes the builder harness, never the task's files: {other[0]} is neither a harness file nor a test"
    return None
