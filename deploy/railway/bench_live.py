#!/usr/bin/env python3
"""Run the offline builder bench (scripts/bench) live on staging.

A ``staging-validator`` program (``VALIDATOR_SCRIPT=bench_live.py``; set
``VALIDATOR_REF`` to the branch to measure). Read-only towards the platform:
its own worktrees under /tmp, no database while the bench runs (POSTGRES_*/
REDIS_* are dropped first, so neither the bench nor its tests can reach one),
no wallets, no GitHub writes, no Society events. Afterwards ONE aggregate row
per run goes to ``society_bench_reports`` (captured settings).

The model is the configured live provider (SOCIETY_MODEL_*, the same
credential boundary as the society-worker). The canary preflight decides
first: anything but LIVE MODEL READY (a scripted/fake provider, an unsafe
credential, an unreachable provider) refuses the run.

    BENCH-PREFLIGHT <verdict> model=<name>
    BENCH TASK {...}        one line per task (structural; no model text)
    BENCH SUMMARY {...}     pass@1/pass@k overall and per split, cost, result classes
    BENCH VERDICT {...}     (BENCH_HARNESS_REF only) the harness-change merge rule
    BENCH RESULT: OK | REFUSED

Env: BENCH_BUDGET_USD (default 1), BENCH_ONLY (comma-separated task ids),
BENCH_PATH (maintenance | society; default maintenance), BENCH_REPEAT (default 3),
BENCH_SPLIT (dev | holdout | all; default all), BENCH_HARNESS_REF (a candidate
branch benched next to this revision, both judged by THIS revision; split all).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
_DB_KEYS = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", REPO, *args], check=True, capture_output=True, text=True, timeout=600).stdout.strip()


def store(db: dict, row: dict) -> None:  # failing to store never fails the bench
    if not db.get("POSTGRES_HOST"):
        print("BENCH-STORE skipped: no database configured", flush=True)
        return
    try:
        import psycopg2  # noqa: PLC0415

        conn = psycopg2.connect(host=db["POSTGRES_HOST"], port=int(db.get("POSTGRES_PORT") or 5432), user=db.get("POSTGRES_USER"),
                                password=db.get("POSTGRES_PASSWORD"), dbname=db.get("POSTGRES_DB"), connect_timeout=10)
        with conn, conn.cursor() as cur:
            cur.execute("INSERT INTO society_bench_reports (id, revision, judge_revision, path, model, repeat, summary, per_task) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (str(uuid.uuid4()), row["revision"], row["judge_revision"], row["path"], row["model"], row["repeat"], json.dumps(row["summary"]), json.dumps(row["per_task"])))
        conn.close()
        print(f"BENCH-STORE ok revision={row['revision'][:12]} path={row['path']}", flush=True)
    except Exception as exc:  # noqa: BLE001 -- never print connection details
        print(f"BENCH-STORE failed: {type(exc).__name__}", flush=True)


def _run(bench, argv: list, out: str, harness_root: str = "") -> dict:
    if harness_root:  # the candidate harness in its own process: this revision's judge, the candidate's services/
        env = {**os.environ, "BENCH_HARNESS_ROOT": harness_root, "PYTHONPATH": REPO}
        rc = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "bench", "run.py"), *argv, "--json-out", out], env=env, timeout=7200).returncode
    else:
        rc = bench.main(argv + ["--json-out", out])
    if rc != 0 or not os.path.exists(out):
        raise RuntimeError(f"bench exited {rc}")
    with open(out, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    db = {k: os.environ.get(k, "") for k in _DB_KEYS}
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
    cand_ref = os.getenv("BENCH_HARNESS_REF", "").strip()
    split = "all" if cand_ref else os.getenv("BENCH_SPLIT", "all")
    tasks = bench.load_tasks(os.getenv("BENCH_ONLY", ""), split)
    # the validator clone is shallow; the tasks are commits of main and need their parents
    subprocess.run(["git", "-C", REPO, "fetch", "-q", "--unshallow", "origin", "+refs/heads/main:refs/remotes/origin/main"], check=True, timeout=600)
    missing = [t["id"] for t in tasks if subprocess.run(["git", "-C", REPO, "cat-file", "-e", t["fix_sha"] + "^"], capture_output=True).returncode]
    print(f"BENCH-REPO tasks={len(tasks)} split={split} missing_commits={missing}", flush=True)
    revision = _git("rev-parse", "HEAD")
    argv = ["--repo", REPO, "--path", os.getenv("BENCH_PATH", "maintenance"), "--repeat", os.getenv("BENCH_REPEAT", "3"), "--split", split]
    argv += ["--only", os.environ["BENCH_ONLY"]] if os.getenv("BENCH_ONLY") else []
    with tempfile.TemporaryDirectory(prefix="bench-live-") as tmp:
        try:
            base = _run(bench, argv, os.path.join(tmp, "base.json"))
            store(db, bench.report_row(base, revision=revision, judge_revision=revision))
            if cand_ref:
                _git("fetch", "-q", "origin", f"+refs/heads/{cand_ref}:refs/remotes/origin/{cand_ref}")
                cand_root = os.path.join(tmp, "candidate")
                _git("worktree", "add", "-q", "--detach", cand_root, f"origin/{cand_ref}")
                cand = _run(bench, argv, os.path.join(tmp, "cand.json"), harness_root=cand_root)
                store(db, bench.report_row(cand, revision=_git("-C", cand_root, "rev-parse", "HEAD"), judge_revision=revision))
                print("BENCH VERDICT " + json.dumps({"candidate_ref": cand_ref, **bench.harness_verdict(base, cand)}, sort_keys=True), flush=True)
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            print(f"BENCH RESULT: REFUSED ({type(exc).__name__}: {str(exc)[:200]})", flush=True)
            return 2
    print("BENCH RESULT: OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
