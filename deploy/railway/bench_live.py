#!/usr/bin/env python3
"""Run the offline builder bench (scripts/bench) live on staging.

A ``staging-validator`` program (``VALIDATOR_SCRIPT=bench_live.py``; set
``VALIDATOR_REF`` to the branch to measure). Read-only towards the platform:
its own worktrees under /tmp, no database (POSTGRES_*/REDIS_* are dropped from
the environment before anything runs, so neither the bench nor the tests it
runs can reach one), no wallets, no GitHub writes, no Society events.

The model is the configured live provider (SOCIETY_MODEL_*, the same
credential boundary as the society-worker). The canary preflight decides
first: anything but LIVE MODEL READY (a scripted/fake provider, an unsafe
credential, an unreachable provider) refuses the run.

    BENCH-PREFLIGHT <verdict> model=<name>
    BENCH TASK {...}        one line per task (structural; no model text)
    BENCH SUMMARY {...}     pass@1, mean turns, mean test runs, cost, result classes
    BENCH RESULT: OK | REFUSED

Env: BENCH_BUDGET_USD (default 1), BENCH_ONLY (comma-separated task ids),
BENCH_PATH (maintenance | society; default maintenance), BENCH_REPEAT (default 3).
"""

from __future__ import annotations

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)


def main() -> int:
    for k in [k for k in os.environ if k.startswith(("POSTGRES", "REDIS", "DATABASE_URL"))]:
        os.environ.pop(k)
    # Offline tool: no database, no auth, no tokens. The registry config would
    # otherwise demand production secrets just to import the models.
    os.environ["ENVIRONMENT"] = "development"
    from scripts.bench import run as bench  # noqa: PLC0415
    from services.registry.app.society import canary  # noqa: PLC0415

    report = canary.preflight(repo_root=REPO)
    print(f"BENCH-PREFLIGHT {report.verdict} model={report.model_name or '-'}", flush=True)
    if report.verdict != canary.VERDICT_READY:
        print("BENCH RESULT: REFUSED", flush=True)
        return 2
    tasks = bench.load_tasks(os.getenv("BENCH_ONLY", ""))
    # the validator clone is shallow; the tasks are commits of main and need their parents
    subprocess.run(["git", "-C", REPO, "fetch", "-q", "--unshallow", "origin", "+refs/heads/main:refs/remotes/origin/main"], check=True, timeout=600)
    missing = [t["id"] for t in tasks if subprocess.run(["git", "-C", REPO, "cat-file", "-e", t["fix_sha"] + "^"], capture_output=True).returncode]
    print(f"BENCH-REPO tasks={len(tasks)} missing_commits={missing}", flush=True)
    argv = ["--repo", REPO, "--path", os.getenv("BENCH_PATH", "maintenance"), "--repeat", os.getenv("BENCH_REPEAT", "3")]
    rc = bench.main(argv + (["--only", os.environ["BENCH_ONLY"]] if os.getenv("BENCH_ONLY") else []))
    print(f"BENCH RESULT: {'OK' if rc == 0 else 'REFUSED'}", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
