#!/usr/bin/env python3
"""Society staging smoke — stdlib only, read-only unless --inject.

    python3 deploy/society-staging-smoke.py --api http://localhost:8100 [--expect-runtime on|off]
        [--inject] [--wait 180] [--report society-smoke.json] [--metrics-probe HOST:PORT]

Environment (never printed):
    SOCIETY_SMOKE_TOKEN   operator user JWT. Without it only the public checks run and the
                          operator surfaces are verified to refuse anonymous access.

Checks (PASS/FAIL each; exit 0 only when all pass):
    C01 /healthz  C02 /readyz  C03 public status (flags, fleet, production deploy hard OFF)
    C04 runtime flag matches --expect-runtime  C05 public surfaces carry no private fields
    C06 /metrics aggregates  C07 operator surfaces refuse anonymous callers
    C08 operator config never returns the model credential  C09 approvals listing
    C10 budget  C11 --inject: allow-listed event accepted, idempotent replay, story sanitised,
        (runtime on) at least one run completes and reports its model provider
    C12 --metrics-probe: the society worker metrics port is NOT reachable from here
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

PUBLIC = ["/v1/society/status", "/v1/society/metrics", "/v1/society/candidates"]
OPERATOR_ONLY = [
    "/v1/society/config",
    "/v1/society/events",
    "/v1/society/runs",
    "/v1/society/intents",
    "/v1/society/budget",
    "/v1/society/approvals",
    "/v1/society/operators",
    "/v1/society/ask?q=goals",
]
PRIVATE_MARKERS = ("payload", "context_summary", "decision_summary", "content", "memory", "wallet", "balance", "api_key", "workspace_path", "repo_root", "model_base_url", "model_name", "title", "error", "policy_reason", "spec", "qa_report")


class Http:
    def __init__(self, base: str, token: Optional[str], timeout: float = 20.0):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, *, auth: bool = True) -> Tuple[int, Any, str]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if auth and self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            status = exc.code
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            return 0, None, f"transport error: {type(exc).__name__}"
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        return status, parsed, raw


class Smoke:
    def __init__(self, http: Http, has_token: bool):
        self.http = http
        self.has_token = has_token
        self.results: List[Dict[str, Any]] = []

    def check(self, code: str, name: str, passed: bool, note: str = "") -> bool:
        self.results.append({"check": code, "name": name, "pass": bool(passed), "note": note[:300]})
        sys.stdout.write(f"{'PASS' if passed else 'FAIL'} {code} {name}{(' — ' + note[:160]) if note else ''}\n")
        return passed

    def skip(self, code: str, name: str, why: str) -> None:
        self.results.append({"check": code, "name": name, "pass": None, "note": why})
        sys.stdout.write(f"SKIP {code} {name} — {why}\n")

    @property
    def failed(self) -> List[str]:
        return [r["check"] for r in self.results if r["pass"] is False]


def _keys(obj: Any, acc: set) -> set:
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.add(str(k))
            _keys(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _keys(v, acc)
    return acc


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.getenv("SOCIETY_SMOKE_API_URL", "http://localhost:8100"))
    ap.add_argument("--expect-runtime", choices=["on", "off", "true", "false"], help="assert the public runtime_enabled flag")
    ap.add_argument("--inject", action="store_true", help="inject one staging.canary.signal (needs the token)")
    ap.add_argument("--wait", type=float, default=180.0, help="seconds to wait for a run after --inject")
    ap.add_argument("--report", help="write the JSON report here")
    ap.add_argument("--metrics-probe", help="HOST:PORT that must NOT accept connections (society metrics)")
    args = ap.parse_args(argv)

    token = os.getenv("SOCIETY_SMOKE_TOKEN") or None
    http = Http(args.api, token)
    s = Smoke(http, token is not None)
    sys.stdout.write(f"society smoke against {args.api} (token: {'present' if token else 'absent'})\n")

    st, _, raw = http.call("GET", "/healthz", auth=False)
    s.check("C01", "GET /healthz", st == 200, raw[:80] if st != 200 else "")
    st, _, raw = http.call("GET", "/readyz", auth=False)
    s.check("C02", "GET /readyz", st == 200, raw[:80] if st != 200 else "")

    st, status, raw = http.call("GET", "/v1/society/status", auth=False)
    ok = st == 200 and isinstance(status, dict) and status.get("production_deploy_enabled") is False and len(status.get("fleet") or []) >= 6 and "model_provider" in status
    s.check("C03", "public status: production deploy OFF, fleet present", ok, "" if ok else raw[:200])
    if args.expect_runtime:
        want = args.expect_runtime in ("on", "true")
        got = bool(isinstance(status, dict) and status.get("runtime_enabled"))
        s.check("C04", f"runtime_enabled == {want}", got == want, f"observed runtime_enabled={got}")
    else:
        s.skip("C04", "runtime flag", "--expect-runtime not given")

    leaked: List[str] = []
    for path in PUBLIC:
        st, body, raw = http.call("GET", path, auth=False)
        if st != 200:
            leaked.append(f"{path} -> HTTP {st}")
            continue
        keys = _keys(body, set())
        for m in PRIVATE_MARKERS:
            if m in keys:
                leaked.append(f"{path} exposes key {m!r}")
    s.check("C05", "public surfaces carry no private fields", not leaked, "; ".join(leaked))

    st, metrics, raw = http.call("GET", "/v1/society/metrics", auth=False)
    s.check("C06", "public metrics aggregates", st == 200 and isinstance(metrics, dict) and "runs" in metrics and "events" in metrics, raw[:120] if st != 200 else "")

    anon_bad = []
    for path in OPERATOR_ONLY:
        st, _, _ = http.call("GET", path, auth=False)
        if st != 401:
            anon_bad.append(f"{path} -> {st}")
    s.check("C07", "operator surfaces refuse anonymous callers (401)", not anon_bad, "; ".join(anon_bad))

    if token:
        st, cfg, raw = http.call("GET", "/v1/society/config")
        settings = (cfg or {}).get("settings", {}) if isinstance(cfg, dict) else {}
        ok = st == 200 and settings.get("model_api_key") in ("", "***") and settings.get("production_deploy_enabled") is False
        s.check("C08", "operator config redacts the credential; production deploy OFF", ok, "" if ok else (raw[:160] if st != 200 else "credential or flag exposed"))
        st, appr, raw = http.call("GET", "/v1/society/approvals")
        s.check("C09", "operator approvals listing", st == 200 and isinstance(appr, dict) and "pending" in appr and "approved_waiting_resume" in appr, raw[:120] if st != 200 else f"pending={len((appr or {}).get('pending', []))}")
        st, budget, raw = http.call("GET", "/v1/society/budget")
        s.check("C10", "operator budget", st == 200 and isinstance(budget, dict) and "model_spend_today_usd" in budget, raw[:120] if st != 200 else f"spend_today={(budget or {}).get('model_spend_today_usd')}")
    else:
        for code, name in (("C08", "operator config"), ("C09", "operator approvals"), ("C10", "operator budget")):
            s.skip(code, name, "SOCIETY_SMOKE_TOKEN not set")

    if args.inject and token:
        tag = uuid.uuid4().hex[:8]
        marker = f"SMOKE-PRIVATE-{tag}"
        corr = str(uuid.uuid4())
        body = {"event_type": "staging.canary.signal", "payload": {"signal": "smoke", "tag": tag, "note": marker}, "correlation_id": corr, "idempotency_key": f"smoke-{tag}"}
        st, ev, raw = http.call("POST", "/v1/society/events", body)
        st2, ev2, raw2 = http.call("POST", "/v1/society/events", body)
        ok = st == 201 and isinstance(ev, dict) and ev.get("duplicate") is False and st2 == 200 and isinstance(ev2, dict) and ev2.get("duplicate") is True
        s.check("C11a", "world event accepted once, replay is a duplicate", ok, f"first={st} replay={st2} {raw[:100] if st != 201 else ''}")
        runtime_on = bool(isinstance(status, dict) and status.get("runtime_enabled"))
        deadline = time.monotonic() + (args.wait if runtime_on else 5)
        story: Dict[str, Any] = {}
        completed: List[Dict[str, Any]] = []
        while time.monotonic() < deadline:
            st, story, raw = http.call("GET", f"/v1/society/story/{corr}", auth=False)
            runs = (story or {}).get("runs") or [] if isinstance(story, dict) else []
            completed = [r for r in runs if r.get("status") == "completed"]
            if completed or not runtime_on:
                break
            time.sleep(3)
        pub_raw = json.dumps(story)
        s.check("C11b", "public story is sanitised (no payload / marker)", marker not in pub_raw and "payload" not in _keys(story, set()), "")
        if runtime_on:
            providers = sorted({str(r.get("model_provider")) for r in completed})
            s.check("C11c", "at least one run completed for the injected event", bool(completed), f"completed={len(completed)} providers={providers}")
            st, detail, raw = http.call("GET", f"/v1/society/story/{corr}/detail")
            ok = st == 200 and isinstance(detail, dict) and all(r.get("model_provider") for r in detail.get("runs") or [])
            s.check("C11d", "operator detail reports the model provider per run", ok, raw[:120] if st != 200 else "")
        else:
            st, story, raw = http.call("GET", f"/v1/society/story/{corr}", auth=False)
            s.check("C11c", "event persisted as pending while runtime is off", st == 200 and isinstance(story, dict) and any(e.get("status") == "pending" for e in story.get("events") or []), raw[:120])
    elif args.inject:
        s.skip("C11", "inject", "SOCIETY_SMOKE_TOKEN not set")
    else:
        s.skip("C11", "inject", "--inject not given")

    if args.metrics_probe:
        host, _, port = args.metrics_probe.rpartition(":")
        reachable = False
        try:
            with socket.create_connection((host, int(port)), timeout=3):
                reachable = True
        except OSError:
            reachable = False
        s.check("C12", f"society metrics port {args.metrics_probe} is not reachable", not reachable, "")
    else:
        s.skip("C12", "metrics port probe", "--metrics-probe not given")

    report = {"api": args.api, "checks": s.results, "failed": s.failed, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    sys.stdout.write(f"\nSOCIETY SMOKE: {'PASS' if not s.failed else 'FAIL ' + ','.join(s.failed)}\n")
    return 0 if not s.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
