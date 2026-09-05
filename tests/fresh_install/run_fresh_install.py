#!/usr/bin/env python3
"""Fresh-install and restart/upgrade proof for AgentNet — process based (no Docker daemon needed).

    python tests/fresh_install/run_fresh_install.py [--snapshot none|pre-society] [--keep] [--report PATH]

What it proves, from a clean copy of the working tree and a database that did not exist:
  1. copy the repository into a temporary directory and turn it into a fresh git repo
     (the Builder needs a checkout to branch from; no historical machine state is used);
  2. create an EMPTY PostgreSQL database (or, with --snapshot pre-society, a database at the
     pre-society schema populated with legacy rows) and run the registry container entrypoint
     logic (`python -m app.db_bootstrap` + `alembic stamp/upgrade`) exactly as a deployment would;
  3. start registry, payment, worker and the society worker as processes with generated secrets;
  4. wait for /healthz and /readyz on both APIs;
  5. run tests/test_integration.py (register → agent → wallet → task/escrow → worker refund) against them;
  6. run the deterministic society end-to-end demo (one event → candidate READY);
  7. stop everything, re-run the entrypoint logic (must be a no-op), start again, verify the data
     persisted, readiness holds and the entrypoint/seed are idempotent;
  8. drop the scratch database (unless --keep) and write a JSON report.

Requirements: PostgreSQL reachable via POSTGRES_HOST/PORT/USER/PASSWORD (a superuser or a role that
may CREATE DATABASE), `redis-server` on PATH (a private instance is started on a free port), `alembic`
and `uvicorn` importable in this interpreter. Exit code 0 only when every step passed.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
IGNORE = shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".pytest_cache", ".venv", "legacy", "werewolf_data", "backups", "*.mp4", "*.pptx", "*.docx", ".hermes")
PRE_SOCIETY_FILES = ("01-", "02-", "03-", "03_", "04-", "05-", "06-", "07-", "08-", "09-", "10-", "11-", "12-", "13-", "14-", "15-")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read().decode()


def wait_http(url: str, seconds: float, want: int = 200) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            st, _ = http(url)
            if st == want:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


class Harness:
    def __init__(self, args):
        self.args = args
        self.pg = {
            "host": os.getenv("POSTGRES_HOST", "127.0.0.1"),
            "port": os.getenv("POSTGRES_PORT", "5432"),
            "user": os.getenv("POSTGRES_USER", "agentnet"),
            "password": os.getenv("POSTGRES_PASSWORD", ""),
        }
        self.tag = time.strftime("%Y%m%d%H%M%S")
        self.dbname = f"agentnet_fresh_{self.tag}"
        self.work = pathlib.Path(args.workdir or f"/tmp/agentnet-fresh-{self.tag}")
        self.copy = self.work / "repo"
        self.procs: dict[str, subprocess.Popen] = {}
        self.redis_port = free_port()
        self.redis_password = secrets.token_hex(16)
        self.ports = {"registry": free_port(), "payment": free_port(), "worker_metrics": free_port(), "society_metrics": free_port()}
        self.steps: list[dict] = []
        self.jwt_secret = secrets.token_hex(32)

    # ── helpers ─────────────────────────────────────────────────────────

    def step(self, name: str, ok: bool, note: str = "") -> bool:
        self.steps.append({"step": name, "ok": bool(ok), "note": note[:2000]})
        sys.stdout.write(f"{'PASS' if ok else 'FAIL'} {name}{(' — ' + note[:200]) if note else ''}\n")
        sys.stdout.flush()
        return ok

    def env(self, **extra) -> dict:
        e = {k: v for k, v in os.environ.items() if not k.startswith(("POSTGRES_", "REDIS_", "SOCIETY_", "JWT_", "ENVIRONMENT"))}
        e.update({
            "ENVIRONMENT": "development",
            "POSTGRES_HOST": self.pg["host"], "POSTGRES_PORT": self.pg["port"], "POSTGRES_USER": self.pg["user"],
            "POSTGRES_PASSWORD": self.pg["password"], "POSTGRES_DB": self.dbname,
            "REDIS_HOST": "127.0.0.1", "REDIS_PORT": str(self.redis_port), "REDIS_PASSWORD": self.redis_password,
            "JWT_SECRET_KEY": self.jwt_secret, "JAEGER_ENABLED": "false", "PYTHONUNBUFFERED": "1",
            "PUBLIC_BASE_URL": f"http://127.0.0.1:{self.ports['registry']}",
            "INTERNAL_WORKER_TOKEN": secrets.token_hex(16),
        })
        e.update(extra)
        return e

    def psql(self, sql: str, db: str = "postgres") -> str:
        cmd = ["psql", "-h", self.pg["host"], "-p", self.pg["port"], "-U", self.pg["user"], "-d", db, "-v", "ON_ERROR_STOP=1", "-tA", "-c", sql]
        return subprocess.run(cmd, env={**os.environ, "PGPASSWORD": self.pg["password"]}, capture_output=True, text=True, check=True).stdout.strip()

    def run(self, cmd, cwd, env=None, timeout=900, log: str | None = None) -> subprocess.CompletedProcess:
        r = subprocess.run(cmd, cwd=str(cwd), env=env or self.env(), capture_output=True, text=True, timeout=timeout)
        if log:
            # Keep the full inner output next to the service logs so a failing
            # step can be diagnosed from the work dir (and from CI artifacts).
            (self.work / f"{log}.log").write_text(f"$ {' '.join(map(str, cmd))}\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}\n", encoding="utf-8")
        return r

    def spawn(self, name: str, cmd, cwd, env=None, log=None):
        logf = open(self.work / f"{name}.log", "a")
        self.procs[name] = subprocess.Popen(cmd, cwd=str(cwd), env=env or self.env(), stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)

    def stop_all(self, names=None):
        for name in list(names or self.procs):
            p = self.procs.pop(name, None)
            if p is None:
                continue
            try:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait(timeout=20)
            except Exception:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except Exception:
                    pass

    # ── steps ──────────────────────────────────────────────────────────

    def prepare_copy(self):
        self.work.mkdir(parents=True, exist_ok=True)
        shutil.copytree(ROOT, self.copy, ignore=IGNORE)
        git = ["git", "-c", "user.email=fresh@agentnet.test", "-c", "user.name=fresh-install"]
        subprocess.run(git + ["init", "-q", "-b", "main"], cwd=self.copy, check=True, capture_output=True)
        subprocess.run(git + ["add", "-A"], cwd=self.copy, check=True, capture_output=True)
        subprocess.run(git + ["commit", "-q", "-m", "fresh install snapshot"], cwd=self.copy, check=True, capture_output=True)
        self.step("copy working tree into a fresh git repository", True, str(self.copy))

    def start_redis(self):
        (self.work / "redis").mkdir(exist_ok=True)
        self.spawn("redis", ["redis-server", "--port", str(self.redis_port), "--requirepass", self.redis_password, "--dir", str(self.work / "redis"), "--save", ""], self.work)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.redis_port), timeout=1).close()
                return self.step("private redis started", True, f"port {self.redis_port}")
            except OSError:
                time.sleep(0.3)
        return self.step("private redis started", False, "not listening")

    def create_database(self):
        self.psql(f'CREATE DATABASE "{self.dbname}"')
        if self.args.snapshot == "pre-society":
            init = sorted((self.copy / "services/registry/init-db").glob("*.sql"))
            for f in init:
                if f.name.startswith(PRE_SOCIETY_FILES):
                    subprocess.run(["psql", "-h", self.pg["host"], "-p", self.pg["port"], "-U", self.pg["user"], "-d", self.dbname, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(f)], env={**os.environ, "PGPASSWORD": self.pg["password"]}, check=True, capture_output=True)
            # legacy rows a real pre-society deployment would hold
            uid, aid, wid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            self.psql(f"INSERT INTO users (id,email,password_hash) VALUES ('{uid}','legacy@fresh.test','x')", self.dbname)
            self.psql(f"INSERT INTO agents (id,user_id,name,capabilities,endpoint,public_key,status) VALUES ('{aid}','{uid}','Legacy_Agent','[]'::jsonb,'http://legacy','k','active')", self.dbname)
            self.psql(f"INSERT INTO wallets (id,owner_type,owner_id,balance_credits,reserved_credits,spending_cap) VALUES ('{wid}','agent','{aid}',250,0,1000)", self.dbname)
            self.legacy = {"user": str(uid), "agent": str(aid), "wallet": str(wid)}
            return self.step("database created at the PRE-SOCIETY snapshot with legacy rows", True, self.dbname)
        return self.step("EMPTY database created", True, self.dbname)

    def entrypoint(self, label: str):
        """Exactly what services/registry/entrypoint.sh does, via its Python module + alembic."""
        reg = self.copy / "services/registry"
        env = self.env(INIT_DB_DIR=str(reg / "init-db"))
        r = self.run(["bash", "entrypoint.sh", "true"], reg, env=env)
        ok = r.returncode == 0
        current = self.run(["alembic", "current"], reg, env=env)
        head_ok = "(head)" in current.stdout
        return self.step(f"registry entrypoint ({label})", ok and head_ok, (r.stdout + r.stderr)[-300:] if not ok else current.stdout.strip().splitlines()[0])

    def start_services(self):
        reg = self.copy / "services/registry"
        self.spawn("registry", [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.ports["registry"])], reg, env=self.env(ORCHESTRATOR_ENABLED="false"))
        self.spawn("payment", [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.ports["payment"])], self.copy / "services/payment")
        self.spawn("worker", [sys.executable, "-m", "app.worker"], self.copy / "services/worker", env=self.env(WORKER_METRICS_PORT=str(self.ports["worker_metrics"]), WORKER_POLL_INTERVAL_SEC="2"))
        self.spawn_society_worker()
        ok = True
        for name, port in (("registry", self.ports["registry"]), ("payment", self.ports["payment"])):
            ok &= self.step(f"{name} /healthz", wait_http(f"http://127.0.0.1:{port}/healthz", 90))
            ok &= self.step(f"{name} /readyz (db + redis)", wait_http(f"http://127.0.0.1:{port}/readyz", 60))
        ok &= self.step("worker metrics endpoint", wait_http(f"http://127.0.0.1:{self.ports['worker_metrics']}/metrics", 60))
        ok &= self.step("society worker metrics endpoint", wait_http(f"http://127.0.0.1:{self.ports['society_metrics']}/metrics", 60))
        return ok

    def spawn_society_worker(self):
        reg = self.copy / "services/registry"
        self.spawn("society-worker", [sys.executable, "-m", "app.society.worker"], reg, env=self.env(SOCIETY_RUNTIME_ENABLED="true", SOCIETY_MODEL_PROVIDER="scripted", SOCIETY_AUTONOMOUS_CODE_ENABLED="false", SOCIETY_METRICS_PORT=str(self.ports["society_metrics"]), SOCIETY_HEARTBEAT_INTERVAL_SECONDS="0"))

    def integration_tests(self):
        env = self.env(REGISTRY_URL=f"http://127.0.0.1:{self.ports['registry']}", PAYMENT_URL=f"http://127.0.0.1:{self.ports['payment']}")
        r = self.run([sys.executable, "-m", "pytest", "tests/test_integration.py", "-p", "no:cacheprovider", "-o", "addopts=--tb=short", "-q", "-rs"], self.copy, env=env, timeout=900, log="integration-tests")
        summary = [l for l in r.stdout.splitlines() if "passed" in l or "failed" in l or "error" in l]
        ok = r.returncode == 0 and "skipped" not in (summary[-1] if summary else "")
        note = summary[-1] if summary else r.stdout[-300:]
        if not ok:
            failures = [l for l in r.stdout.splitlines() if l.startswith(("FAILED", "ERROR", "E   "))]
            note += " | " + " ; ".join(failures[:8])[:1200] + f" | full output: {self.work / 'integration-tests.log'}"
        return self.step("tests/test_integration.py against the live processes", ok, note)

    def society_demo(self):
        # The demo drives ONE in-process worker with autonomous code enabled.
        # The long-running society-worker started above runs with the safe
        # default (SOCIETY_AUTONOMOUS_CODE_ENABLED=false); two replicas with
        # different policy on one database is not a supported topology, so
        # the daemon is paused for the demo and restarted right after (its
        # restart is then covered again by the persistence phase).
        self.stop_all(["society-worker"])
        env = self.env(SOCIETY_REPO_ROOT=str(self.copy), SOCIETY_WORKSPACE_ROOT=str(self.work / "workspaces"), SOCIETY_METRICS_PORT="0")
        r = self.run([sys.executable, "examples/demo_autonomous_society.py", "--max-cycles", "60"], self.copy, env=env, timeout=900, log="society-demo")
        line = [l for l in r.stdout.splitlines() if l.startswith("RESULT:")]
        note = line[-1] if line else (r.stdout + r.stderr)[-300:]
        if r.returncode != 0:
            note += " | " + (r.stdout + r.stderr)[-1200:].replace("\n", " ⏎ ") + f" | full output: {self.work / 'society-demo.log'}"
        ok = self.step("deterministic society E2E (one event → candidate READY)", r.returncode == 0, note)
        self.spawn_society_worker()
        ok &= self.step("society worker metrics endpoint (after demo)", wait_http(f"http://127.0.0.1:{self.ports['society_metrics']}/metrics", 60))
        return ok

    def snapshot_counts(self):
        return {t: int(self.psql(f"SELECT count(*) FROM {t}", self.dbname)) for t in ("users", "agents", "wallets", "task_sessions", "transactions", "society_events", "agent_runs", "code_candidates")}

    def wallet_invariants(self):
        neg = int(self.psql("SELECT count(*) FROM wallets WHERE balance_credits < 0 OR reserved_credits < 0 OR reserved_credits > balance_credits", self.dbname))
        stuck = int(self.psql("SELECT count(*) FROM task_sessions WHERE status IN ('completed','refunded','failed','timeout') AND id IN (SELECT id FROM task_sessions WHERE status='initiated')", self.dbname))
        return self.step("wallet invariants (no negative balance, reserved <= balance)", neg == 0 and stuck == 0, f"violations={neg}")

    def restart(self, before: dict):
        self.stop_all(["registry", "payment", "worker", "society-worker"])
        self.step("clean shutdown of all services", True)
        ok = self.entrypoint("restart: must be a no-op")
        ok &= self.start_services()
        after = self.snapshot_counts()
        persisted = all(after[k] >= before[k] for k in before) and after["users"] == before["users"]
        ok &= self.step("data persisted across restart", persisted, json.dumps(after))
        if self.args.snapshot == "pre-society":
            bal = self.psql(f"SELECT balance_credits FROM wallets WHERE id='{self.legacy['wallet']}'", self.dbname)
            ok &= self.step("legacy wallet balance intact after upgrade", bal == "250", f"balance={bal}")
        # seeding the fleet again must reuse, never duplicate
        r = self.run([sys.executable, "-m", "app.society.seed"], self.copy / "services/registry")
        agents_after = int(self.psql("SELECT count(*) FROM agents", self.dbname))
        ok &= self.step("society seed is idempotent", r.returncode == 0 and agents_after == after["agents"], f"agents={agents_after}")
        return ok

    def cleanup(self):
        self.stop_all()
        if not self.args.keep:
            try:
                self.psql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{self.dbname}' AND pid<>pg_backend_pid()")
                self.psql(f'DROP DATABASE IF EXISTS "{self.dbname}"')
                shutil.rmtree(self.work, ignore_errors=True)
            except Exception as exc:  # noqa: BLE001
                sys.stdout.write(f"cleanup warning: {exc}\n")

    def main(self) -> int:
        ok = True
        try:
            self.prepare_copy()
            ok &= self.start_redis()
            ok &= self.create_database()
            ok &= self.entrypoint("first boot")
            ok &= self.start_services()
            if ok:
                ok &= self.integration_tests()
                ok &= self.society_demo()
                ok &= self.wallet_invariants()
                before = self.snapshot_counts()
                ok &= self.restart(before)
                ok &= self.wallet_invariants()
        except Exception as exc:  # noqa: BLE001
            ok = self.step("harness crashed", False, f"{type(exc).__name__}: {exc}")
        finally:
            report = {"database": self.dbname, "snapshot": self.args.snapshot, "steps": self.steps, "ok": bool(ok), "workdir": str(self.work)}
            if self.args.report:
                report_path = pathlib.Path(self.args.report)
                report_path.write_text(json.dumps(report, indent=2))
                # Service and inner-step logs survive cleanup next to the report
                # (CI uploads that directory as an artifact).
                logs_dir = report_path.with_suffix("") .parent / f"{report_path.stem}-logs"
                logs_dir.mkdir(parents=True, exist_ok=True)
                for log in self.work.glob("*.log"):
                    shutil.copy2(log, logs_dir / log.name)
                report["logs"] = str(logs_dir)
                report_path.write_text(json.dumps(report, indent=2))
            self.cleanup()
        sys.stdout.write(f"\nFRESH INSTALL ({self.args.snapshot}): {'PASS' if ok else 'FAIL'}\n")
        return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", choices=["none", "pre-society"], default="none")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--report")
    ap.add_argument("--workdir")
    raise SystemExit(Harness(ap.parse_args()).main())
