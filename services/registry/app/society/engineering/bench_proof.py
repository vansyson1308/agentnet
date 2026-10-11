"""QA's bench proof for a builder-harness candidate (``bench:<task>`` acceptance).

A bench ticket says the builder harness fails a dev bench task; its proof is
that the CANDIDATE harness delivers it. QA re-runs the task ``REPEAT`` times
with the existing bench runner (``scripts/bench/run.py``, the BENCH_HARNESS_ROOT
path ``bench_live.py`` uses for BENCH_HARNESS_REF): this revision's judge and
task list, the candidate worktree's ``services/``. Passed = delivered on at
least ``MIN_DELIVERED`` runs AND more than the task's baseline (the latest main
report). The numbers ride the QA report and the PR body.

Boundaries:

* only dev tasks of the RUNNING revision's ``tasks.json``; a holdout id is refused;
* the child runs model-authored harness code, so it never holds the model
  credential: it reaches the provider through ``ModelRelay`` on 127.0.0.1 (a
  per-run token, ``/chat/completions`` only, a request cap). No database,
  GitHub, release or signing value reaches it (allowlisted environment);
* bounded by ``SOCIETY_BENCH_PROOF_BUDGET_USD`` and ``..._TIMEOUT_SECONDS``;
  the caller's lease is heartbeated while it runs.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

PREFIX = "bench:"
REPEAT = 3
MIN_DELIVERED = 2
_PASS = re.compile(r"^(PATH|HOME|LANG|LC_\w+|PYTHON\w*|SSL_CERT_\w+|REQUESTS_CA_BUNDLE|HTTPS?_PROXY|NO_PROXY|SOCIETY_MODEL_\w+|MAINTENANCE_\w+)$")
_CREDENTIAL = re.compile(r"(TOKEN|SECRET|PASSWORD|_KEY|CREDENTIAL|ATTESTATION)")
MAX_BODY = 4 * 1024 * 1024


def proofs(spec: Dict[str, Any]) -> List[str]:
    return [t[len(PREFIX):] for t in spec.get("acceptance_tests") or [] if isinstance(t, str) and t.startswith(PREFIX)]


class ModelRelay:
    """Forwards ``POST /chat/completions`` to the real provider with the real key.
    The child gets a loopback URL and a one-off token instead of the credential."""

    def __init__(self, base_url: str, api_key: str, *, max_requests: int = 400, timeout: float = 180.0):
        self.base_url, self._key, self.max_requests, self.timeout = base_url.rstrip("/"), api_key, max_requests, timeout
        self.token = secrets.token_urlsafe(24)
        self.requests = 0
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "ModelRelay":
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # never log requests (bodies are model traffic)
                return

            def _reply(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                auth = self.headers.get("Authorization") or ""
                if self.path.rstrip("/") != "/chat/completions" or not hmac.compare_digest(auth, f"Bearer {relay.token}"):
                    return self._reply(403, b'{"error": "relay: forbidden"}')
                size = int(self.headers.get("Content-Length") or 0)
                with relay._lock:
                    if relay.requests >= relay.max_requests or size > MAX_BODY:
                        return self._reply(429, b'{"error": "relay: request budget used up"}')
                    relay.requests += 1
                body = self.rfile.read(size)
                req = urllib.request.Request(relay.base_url + "/chat/completions", data=body, method="POST",
                                             headers={"Authorization": f"Bearer {relay._key}", "Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=relay.timeout) as resp:  # noqa: S310 -- the configured provider URL
                        return self._reply(resp.status, resp.read())
                except urllib.error.HTTPError as exc:
                    return self._reply(exc.code, exc.read()[:2000])
                except Exception:  # noqa: BLE001 -- never echo the request
                    return self._reply(502, b'{"error": "relay: provider unreachable"}')

            def do_GET(self) -> None:  # noqa: N802
                self._reply(403, b'{"error": "relay: forbidden"}')

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, name="bench-model-relay", daemon=True).start()
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


def child_env(settings: Any, relay: ModelRelay, harness_root: str, environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {k: v for k, v in (os.environ if environ is None else environ).items() if _PASS.match(k) and not _CREDENTIAL.search(k)}
    no_proxy = ",".join(x for x in (env.get("NO_PROXY", ""), "127.0.0.1", "localhost") if x)
    env.update({"SOCIETY_MODEL_PROVIDER": "openai_compatible", "SOCIETY_MODEL_BASE_URL": relay.url, "SOCIETY_MODEL_API_KEY": relay.token,
                "NO_PROXY": no_proxy, "no_proxy": no_proxy, "ENVIRONMENT": "development", "BENCH_HARNESS_ROOT": harness_root,
                "PYTHONPATH": settings.repo_root, "BENCH_BUDGET_USD": str(settings.bench_proof_budget_usd)})
    return env


def _git(repo: str, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=timeout, check=False)


def _dev_task(repo_root: str, task_id: str) -> Optional[Dict[str, Any]]:
    try:
        with open(os.path.join(repo_root, "scripts", "bench", "tasks.json"), encoding="utf-8") as f:
            return next((t for t in json.load(f)["tasks"] if t.get("id") == task_id), None)
    except (OSError, ValueError, KeyError):
        return None


def run_proof(settings: Any, harness_root: str, task_id: str, *, baseline_delivered: int, heartbeat: Callable[[], None] = lambda: None,
              spawn: Optional[Callable[..., subprocess.Popen]] = None) -> Dict[str, Any]:
    """Run the proof; never raises. Returns the structural result (QA check + PR body)."""
    out: Dict[str, Any] = {"task_id": task_id, "repeat": REPEAT, "baseline_delivered": int(baseline_delivered), "delivered": 0, "runs": [],
                           "cost_usd": "0", "passed": False, "rule": f"delivered >= {MIN_DELIVERED}/{REPEAT} and > baseline", "judge": "running revision"}
    repo = settings.repo_root
    task = _dev_task(repo, task_id)
    if task is None:
        return {**out, "error": "not a dev task of the running revision's scripts/bench/tasks.json"}
    if task.get("fix_sha") and _git(repo, "cat-file", "-e", task["fix_sha"] + "^").returncode:
        _git(repo, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
    if not settings.model_api_key or not settings.model_base_url:
        return {**out, "error": "no live model is configured in this process"}
    with tempfile.TemporaryDirectory(prefix="bench-proof-") as tmp, ModelRelay(settings.model_base_url, settings.model_api_key) as relay:
        report = os.path.join(tmp, "proof.json")
        argv = [sys.executable, os.path.join(repo, "scripts", "bench", "run.py"), "--repo", repo, "--only", task_id, "--repeat", str(REPEAT),
                "--path", "maintenance", "--split", "dev", "--json-out", report]
        proc = (spawn or subprocess.Popen)(argv, cwd=tmp, env=child_env(settings, relay, harness_root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           start_new_session=True)
        deadline = time.monotonic() + settings.bench_proof_timeout_seconds
        while proc.poll() is None:
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait(timeout=30)
                return {**out, "error": f"timed out after {settings.bench_proof_timeout_seconds}s", "relay_requests": relay.requests}
            heartbeat()
            time.sleep(5)
        try:
            with open(report, encoding="utf-8") as f:
                summary = json.load(f)
        except (OSError, ValueError):
            return {**out, "error": f"the bench runner exited {proc.returncode} without a report", "relay_requests": relay.requests}
    v = (summary.get("per_task") or {}).get(task_id) or {}
    delivered, runs = int(v.get("delivered") or 0), [str(r) for r in v.get("runs") or []]
    passed = len(runs) >= REPEAT and delivered >= MIN_DELIVERED and delivered > int(baseline_delivered)
    return {**out, "delivered": delivered, "runs": runs, "cost_usd": str(summary.get("cost_usd") or "0"), "passed": passed,
            "relay_requests": relay.requests, "model": summary.get("model")}


def summary_line(r: Dict[str, Any]) -> str:
    base = f"bench:{r['task_id']} candidate {r.get('delivered', 0)}/{r.get('repeat', REPEAT)} vs baseline {r.get('baseline_delivered', 0)}/{r.get('repeat', REPEAT)}"
    return base + (f" runs={r.get('runs')}" if r.get("runs") else "") + (f" ({r['error']})" if r.get("error") else "")
