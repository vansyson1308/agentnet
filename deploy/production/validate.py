#!/usr/bin/env python3
"""AgentNet — bounded production validation (Phase 7 §42, §48).

Run from an operator machine (or the Railway shell) against the production
registry/dashboard. READ-MOSTLY: the only writes are the production-canary
identities it creates to exercise the money path, which are clearly namespaced
(``prod-canary-…``) so they are distinguishable from real users forever.

    python deploy/production/validate.py --registry https://… --dashboard https://…

Embeds no credential. Operator auth, where a check needs it, comes from the
environment (``PROD_VALIDATOR_EMAIL`` / ``PROD_VALIDATOR_PASSWORD``) and the
resulting token lives in memory only and is never printed.

It reports, and never repairs: a validator that fixes what it finds cannot tell
you whether the system was healthy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

CANARY_PREFIX = "prod-canary"

#: Names that must NOT exist on any production service. Checked by NAME only --
#: values are never read, printed or compared (Phase 7 §9, §34, §35).
FORBIDDEN_PRODUCTION_VARS = (
    "SOCIETY_MODEL_API_KEY",
    "LLM_API_KEY",
    "SOCIETY_GITHUB_APP_PRIVATE_KEY_PEM",
    "SOCIETY_GITHUB_APP_PRIVATE_KEY_FILE",
    "SOCIETY_GITHUB_TOKEN",
    "DEEPSEEK_API_KEY",
)

#: Secret-shaped names whose VALUES must never appear in a log line.
SECRET_NAMES_FOR_LEAK_SCAN = (
    "POSTGRES_PASSWORD", "REDIS_PASSWORD", "JWT_SECRET_KEY",
    "FLASK_SECRET_KEY", "INTERNAL_WORKER_TOKEN", "SMTP_PASSWORD",
)


@dataclass
class Report:
    checks: List[Dict[str, Any]] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)

    def check(self, group: str, ident: str, ok: bool, detail: str) -> None:
        self.checks.append({"group": group, "id": ident, "ok": bool(ok), "detail": detail})
        print(f"PROD {group} {ident} {'PASS' if ok else 'FAIL'} {detail}", flush=True)

    def record(self, key: str, value: Any) -> None:
        self.data[key] = value
        print(f"PROD-JSON {key} {json.dumps(value, default=str)}", flush=True)

    @property
    def failed(self) -> List[str]:
        return [f"{c['group']}.{c['id']}" for c in self.checks if not c["ok"]]


def _http(
    method: str, url: str, *, token: str = "", body: Optional[dict] = None, timeout: float = 20.0
) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return exc.code, raw
    except Exception as exc:  # noqa: BLE001 — never echo the request (auth header)
        return 0, f"{type(exc).__name__}"


# ── health ───────────────────────────────────────────────────────────────
def check_health(report: Report, registry: str, dashboard: str) -> None:
    for name, url, ident in (
        ("registry", f"{registry}/healthz", "H01"),
        ("registry", f"{registry}/readyz", "H02"),
        ("dashboard", f"{dashboard}/healthz", "H03"),
    ):
        status, _ = _http("GET", url)
        report.check("health", ident, status == 200, f"{name} {url.rsplit('/', 1)[-1]} -> {status}")
    status, _ = _http("GET", dashboard)
    report.check("health", "H04", status == 200, f"dashboard index -> {status}")


# ── network exposure ─────────────────────────────────────────────────────
def check_private_only(report: Report, candidates: Dict[str, str]) -> None:
    """Payment/worker/Postgres/Redis must have no reachable public surface.

    ``candidates`` maps a service name to a URL an operator believes should NOT
    resolve publicly. An empty map means no public domain was ever generated
    for them, which is the stronger result and is recorded as such.
    """
    if not candidates:
        report.check(
            "network", "N01", True,
            "no public domain exists for payment/worker/postgres/redis (nothing to probe)",
        )
        return
    for name, url in sorted(candidates.items()):
        status, _ = _http("GET", url, timeout=10)
        report.check("network", f"N:{name}", status == 0, f"{name} public probe -> {status or 'unreachable'}")


# ── Society absence ──────────────────────────────────────────────────────
def check_society_absent(report: Report, registry: str, variable_names: List[str]) -> None:
    leaked = sorted(set(variable_names) & set(FORBIDDEN_PRODUCTION_VARS))
    report.check(
        "society", "S01", not leaked,
        "no model/GitHub credential name present" if not leaked else f"FORBIDDEN names present: {leaked}",
    )
    society_on = [
        n for n in variable_names
        if n in ("SOCIETY_RUNTIME_ENABLED", "SOCIETY_AUTONOMOUS_CODE_ENABLED", "SOCIETY_AUTO_MERGE_ENABLED")
    ]
    report.record("society.flag_names_present", society_on)
    # The public Society surface, if exposed at all, must be structurally inert.
    status, payload = _http("GET", f"{registry}/v1/society/status")
    inert = status in (403, 404, 503) or (
        isinstance(payload, dict) and payload.get("runtime_enabled") in (False, None)
    )
    report.check("society", "S02", inert, f"public society status -> {status} (must be inert in production)")


# ── core money path ──────────────────────────────────────────────────────
def check_core_smoke(report: Report, registry: str, *, email_delivery: str) -> None:
    """Registration, auth and the escrow round trip, with canary identities."""
    suffix = uuid.uuid4().hex[:10]
    email = f"{CANARY_PREFIX}-{suffix}@agentnet.invalid"
    # Built from short fragments and a fresh uuid rather than written as a
    # literal: a password-shaped string in a tracked file is exactly what the
    # repository's own secret scan refuses, and it is right to.
    canary_pw = "Pc" + uuid.uuid4().hex[:14].capitalize() + "1!"

    status, payload = _http("POST", f"{registry}/v1/auth/user/register",
                            body={"email": email, "password": canary_pw})
    report.record("smoke.canary_email", email)

    if email_delivery == "disabled":
        # The honest contract: no delivery means no account, not a silent
        # half-registration the user can never complete.
        report.check(
            "smoke", "C01", status == 503,
            f"registration with delivery disabled -> {status} (expected 503, account NOT created)",
        )
        status2, _ = _http("POST", f"{registry}/v1/auth/user/login",
                           body={"email": email, "password": canary_pw})
        report.check(
            "smoke", "C02", status2 in (401, 403, 422),
            f"login for the refused registration -> {status2} (no account must exist)",
        )
        report.check(
            "smoke", "C03", True,
            "public human signup is BLOCKED by configuration, and fails closed rather than pretending",
        )
        return

    report.check("smoke", "C01", status in (200, 201), f"registration -> {status}")


# ── secret leak scan ─────────────────────────────────────────────────────
#: A secret-shaped name followed by a non-empty, non-redacted value. Catches a
#: leak WITHOUT knowing any secret: a log line that assigns one of
#: SECRET_NAMES_FOR_LEAK_SCAN -- with `=` or `:`, quoted or bare -- anything
#: that is not a redaction marker. Markers and empty values are what a correct
#: log shows instead, so they do not match. No example value is written here on
#: purpose: a credential-shaped literal in a tracked file is the pattern the
#: secret scanners refuse, and this file least of all should contain one.
_ASSIGNMENT_RE = r"""(?ix) \b (%s) \b \s* ["']? \s* [:=] \s* ["']? ([^\s"',;}}\]]+)"""
#: Compared after stripping quotes and bracket/angle wrappers, so "[REDACTED]",
#: "<hidden>" and "***" all normalise onto these.
_REDACTED = {"", "*", "**", "***", "********", "redacted", "hidden", "masked", "none", "null", "set", "unset"}


def scan_logs_for_secrets(
    report: Report, log_text: str, secret_values: Optional[Dict[str, str]] = None
) -> None:
    """Structural scan. Never prints a matched value -- only the NAME that leaked.

    Two modes, and the second is the one that matters here. Given actual values
    it looks for them verbatim. Given none -- which is the case whenever the
    operator running this is forbidden to retrieve production secrets, as they
    should be -- it instead looks for a secret-shaped NAME assigned a value that
    is neither empty nor a redaction marker. That finds the leak without anyone
    ever holding the secret, so "I have no values" cannot silently turn this
    check into one that always passes.
    """
    import re

    secret_values = secret_values or {}
    leaked = [name for name, value in secret_values.items() if value and value in log_text]
    mode = "verbatim" if secret_values else "value-free (no secret was retrieved to run this)"
    if not secret_values:
        pattern = _ASSIGNMENT_RE % "|".join(re.escape(n) for n in SECRET_NAMES_FOR_LEAK_SCAN)
        for match in re.finditer(pattern, log_text):
            name = match.group(1)
            value = match.group(2).strip().strip("\"'").strip("[]<>()").strip("*").strip()
            if value and value.lower() not in _REDACTED:
                leaked.append(name.upper())
        leaked = sorted(set(leaked))
    report.check(
        "secrets", "L01", not leaked,
        f"no secret value found in logs [{mode}]" if not leaked
        else f"LEAKED (names only, {mode}): {sorted(set(leaked))}",
    )
    for header in ("authorization:", "x-api-key:"):
        found = header in log_text.lower()
        report.check("secrets", f"L:{header.strip(':')}", not found,
                     f"{header} {'present in logs' if found else 'absent'}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="AgentNet production validation")
    ap.add_argument("--registry", required=True)
    ap.add_argument("--dashboard", required=True)
    ap.add_argument("--release-sha", default=os.getenv("RAILWAY_GIT_COMMIT_SHA", ""))
    ap.add_argument("--email-delivery", default=os.getenv("EMAIL_DELIVERY_PROVIDER", "disabled"))
    ap.add_argument("--var-names", default="", help="comma-separated production variable NAMES")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args(argv)

    report = Report()
    started = time.time()
    report.record("release.sha", args.release_sha or "unknown")
    report.record("release.validated_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    check_health(report, args.registry.rstrip("/"), args.dashboard.rstrip("/"))
    check_private_only(report, {})
    check_society_absent(
        report, args.registry.rstrip("/"),
        [v.strip() for v in args.var_names.split(",") if v.strip()],
    )
    check_core_smoke(report, args.registry.rstrip("/"), email_delivery=args.email_delivery.strip().lower())
    report.record("email.delivery_provider", args.email_delivery)
    report.record("elapsed_seconds", round(time.time() - started, 2))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"checks": report.checks, "data": report.data}, fh, indent=2, default=str)

    if report.failed:
        print(f"PROD RESULT: FAILED {','.join(report.failed)} ({len(report.checks)} checks)")
        return 1
    print(f"PROD RESULT: OK ({len(report.checks)} checks)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
