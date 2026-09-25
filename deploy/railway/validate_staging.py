#!/usr/bin/env python3
"""Railway staging validator — runs INSIDE the staging environment.

docs/RAILWAY_STAGING.md §7–§12 and §20 as one reproducible program: it is
started as the `staging-validator` service (registry image, start command
clones the repository and runs this file), reaches the public domains through
Railway's edge and the private services through private DNS, proves the schema
head on the database, bootstraps the allow-listed staging operator, runs the
society smoke and red-team scripts, the core application smoke and the
proxy-header spoof test, and prints one line per check:

    CHECK <id> PASS|FAIL <detail>
    VALIDATION RESULT: GREEN | RED <failed ids>

Nothing secret is ever printed: the operator password is derived from
STAGING_VALIDATOR_SECRET, JWTs stay in memory, and the child scripts are
stdlib-only and print PASS/FAIL lines without tokens.

Environment (all optional except the secret and the database):
    REGISTRY_PUBLIC_URL, DASHBOARD_PUBLIC_URL      https://<generated domain>
    REGISTRY_PRIVATE_URL  (http://registry.railway.internal:8000)
    PAYMENT_PRIVATE_URL   (http://payment.railway.internal:8001)
    WORKER_METRICS_URL    (http://worker.railway.internal:9100/metrics)
    SOCIETY_METRICS_URL   (http://society-worker.railway.internal:9101/metrics)
    POSTGRES_HOST/PORT/USER/PASSWORD/DB           reference variables
    STAGING_VALIDATOR_SECRET                      Railway-generated shared secret
    VALIDATOR_OPERATOR_EMAIL                      one of SOCIETY_OPERATOR_BOOTSTRAP_EMAILS
    VALIDATOR_USER_EMAIL                          plain user for the 403 checks
    EXPECTED_ALEMBIC_HEAD (0013_a2a_federation)
    REDTEAM_BURST (40)  SPOOF_BASELINE (220)  SPOOF_FORGED (60)
    VALIDATOR_EXPECT_RUNTIME (off)                 on|off: the public runtime_enabled flag the smoke asserts
"""

from __future__ import annotations

import json
import os
import pathlib
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# ── pure helpers (unit-tested) ─────────────────────────────────────────


def derive_password(secret: str) -> str:
    """A policy-compliant password (>= 12 chars, upper, lower, digit) derived
    from the Railway-generated secret; never logged."""
    core = ("".join(ch for ch in secret if ch.isalnum()) + "x" * 28)[:28]
    return f"A{core}z1"


def analyse_spoof(baseline: List[int], forged: List[int]) -> Tuple[bool, str]:
    """PASS when the forged run hits 429 no later than the baseline run, i.e.
    forged X-Forwarded-For / X-Real-IP headers did not open a fresh bucket."""
    b = next((i for i, s in enumerate(baseline) if s == 429), None)
    f = next((i for i, s in enumerate(forged) if s == 429), None)
    if b is None:
        return False, f"rate limiter never tripped in {len(baseline)} baseline requests"
    if f is None:
        return False, f"forged headers never hit 429 in {len(forged)} requests (fresh bucket => spoofable)"
    return f <= b, f"baseline first 429 at #{b + 1}, forged first 429 at #{f + 1}"


def expected_runtime(value: str) -> str:
    """VALIDATOR_EXPECT_RUNTIME -> the smoke's --expect-runtime argument.
    Only "on" (Phase 5, runtime deliberately enabled) turns the assertion on;
    anything else keeps the historical default of asserting the runtime OFF."""
    return "on" if value.strip().lower() in ("on", "true", "1", "yes") else "off"


def summarise_script_output(text: str) -> List[str]:
    """Only the PASS/FAIL/SKIP and summary lines of the child scripts."""
    keep = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(("PASS ", "FAIL ", "SKIP ", "SOCIETY SMOKE:", "SOCIETY RED-TEAM:", "RED-TEAM:")):
            keep.append(s)
    return keep


# ── infrastructure ─────────────────────────────────────────────────────


class Report:
    def __init__(self) -> None:
        self.failed: List[str] = []
        self.count = 0

    def record(self, code: str, ok: bool, detail: str = "") -> bool:
        self.count += 1
        if not ok:
            self.failed.append(code)
        sys.stdout.write(f"CHECK {code} {'PASS' if ok else 'FAIL'} {detail[:200]}\n")
        sys.stdout.flush()
        return ok


def http(method: str, url: str, *, body: Optional[dict] = None, headers: Optional[Dict[str, str]] = None,
         token: Optional[str] = None, timeout: float = 20.0) -> Tuple[int, str]:
    data = None
    hdrs = {"Accept": "application/json", "User-Agent": "agentnet-staging-validator"}
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception as e:  # DNS, refused, timeout
        return 0, f"{type(e).__name__}: {e}"


def wait_http(url: str, want: int = 200, attempts: int = 8, pause: float = 5.0) -> Tuple[int, str]:
    st, body = 0, ""
    for _ in range(attempts):
        st, body = http("GET", url)
        if st == want:
            return st, body
        time.sleep(pause)
    return st, body


def db_connect():
    import psycopg2  # registry image dependency

    return psycopg2.connect(
        host=env("POSTGRES_HOST"), port=int(env("POSTGRES_PORT", "5432")), user=env("POSTGRES_USER"),
        password=env("POSTGRES_PASSWORD"), dbname=env("POSTGRES_DB"), connect_timeout=15,
    )


def ensure_user(rep: Report, code: str, api: str, email: str, password: str) -> Optional[str]:
    """Register (or reuse), mark verified on the staging database (SMTP is not
    wired), log in. Returns the JWT (never printed)."""
    st, body = http("POST", f"{api}/v1/auth/user/register", body={"email": email, "password": password})
    created = st == 201
    exists = st == 400 and "already registered" in body.lower()
    if not (created or exists):
        # the body is the API's validation/error text (never a credential)
        rep.record(f"{code}a", False, f"register {email.split('@')[0]}: HTTP {st} {body[:120]!r}")
        return None
    rep.record(f"{code}a", True, f"register {email.split('@')[0]}: {'created' if created else 'exists'}")
    try:
        conn = db_connect()
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE users SET is_email_verified = TRUE WHERE lower(email) = lower(%s)", (email,))
            n = cur.rowcount
        conn.close()
        rep.record(f"{code}b", n >= 1, f"email verified on the staging database (rows={n})")
    except Exception as e:
        rep.record(f"{code}b", False, f"verify: {type(e).__name__}")
        return None
    st, body = http("POST", f"{api}/v1/auth/user/login", body={"email": email, "password": password})
    token = None
    if st == 200:
        try:
            token = json.loads(body).get("access_token")
        except Exception:
            token = None
    rep.record(f"{code}c", bool(token), f"login: HTTP {st}")
    return token


def run_child(script: pathlib.Path, args: List[str], extra_env: Dict[str, str]) -> Tuple[int, List[str]]:
    proc = subprocess.run(
        [sys.executable, str(script), *args], env={**os.environ, **extra_env},
        capture_output=True, text=True, timeout=900,
    )
    lines = summarise_script_output(proc.stdout + "\n" + proc.stderr)
    return proc.returncode, lines


def spoof_test(api: str, baseline_n: int, forged_n: int) -> Tuple[List[int], List[int]]:
    url = f"{api}/v1/auth/user/login"
    bad = {"email": "nobody@agentnet.invalid", "password": "definitely-wrong-password-1A"}
    baseline = [http("POST", url, body=bad, timeout=15)[0] for _ in range(baseline_n)]
    forged: List[int] = []
    for _ in range(forged_n):
        hdrs = {
            "X-Forwarded-For": f"203.0.113.{random.randint(1, 254)}",
            "X-Real-IP": f"198.51.100.{random.randint(1, 254)}",
        }
        forged.append(http("POST", url, body=bad, headers=hdrs, timeout=15)[0])
    return baseline, forged


# ── main ───────────────────────────────────────────────────────────────


def main() -> int:
    rep = Report()
    api = env("REGISTRY_PUBLIC_URL").rstrip("/")
    dash = env("DASHBOARD_PUBLIC_URL").rstrip("/")
    reg_priv = env("REGISTRY_PRIVATE_URL", "http://registry.railway.internal:8000").rstrip("/")
    pay_priv = env("PAYMENT_PRIVATE_URL", "http://payment.railway.internal:8001").rstrip("/")
    worker_metrics = env("WORKER_METRICS_URL", "http://worker.railway.internal:9100/metrics")
    society_metrics = env("SOCIETY_METRICS_URL", "http://society-worker.railway.internal:9101/metrics")
    secret = env("STAGING_VALIDATOR_SECRET")
    op_email = env("VALIDATOR_OPERATOR_EMAIL", "staging-operator@staging.agentnet.io.vn")
    user_email = env("VALIDATOR_USER_EMAIL", "staging-user@staging.agentnet.io.vn")
    expected_head = env("EXPECTED_ALEMBIC_HEAD", "0013_a2a_federation")
    expect_runtime = expected_runtime(env("VALIDATOR_EXPECT_RUNTIME"))

    sys.stdout.write(
        f"VALIDATOR start deployment={env('RAILWAY_DEPLOYMENT_ID', '?')[:8]} commit={env('RAILWAY_GIT_COMMIT_SHA', '?')[:12]} "
        f"operator={op_email.split('@')[0]} api={api} dashboard={dash}\n"
    )
    if not api or not dash or not secret:
        rep.record("V00", False, "REGISTRY_PUBLIC_URL, DASHBOARD_PUBLIC_URL and STAGING_VALIDATOR_SECRET are required")
        return finish(rep)
    rep.record("V00", len(secret) >= 32, f"validator secret present ({len(secret)} chars)")

    # §7 health matrix — public through the edge, private through private DNS
    st, body = wait_http(f"{api}/healthz")
    rep.record("H01", st == 200, f"registry public /healthz HTTP {st}")
    st, body = wait_http(f"{api}/readyz")
    rep.record("H02", st == 200 and '"ready"' in body, f"registry public /readyz HTTP {st}")
    st, body = wait_http(f"{dash}/healthz")
    rep.record("H03", st == 200, f"dashboard public /healthz HTTP {st}")
    st, body = wait_http(f"{dash}/readyz")
    rep.record("H04", st == 200 and '"ready"' in body, f"dashboard /readyz (dashboard -> registry over private DNS) HTTP {st}")
    st, body = wait_http(f"{pay_priv}/readyz")
    rep.record("H05", st == 200 and '"ready"' in body, f"payment private /readyz HTTP {st}")
    st, body = wait_http(worker_metrics)
    rep.record("H06", st == 200 and "python_info" in body, f"worker private /metrics HTTP {st}")
    st, body = wait_http(society_metrics)
    rep.record("H07", st == 200 and "python_info" in body, f"society-worker private /metrics HTTP {st}")
    st, body = wait_http(f"{reg_priv}/healthz")
    rep.record("H08", st == 200, f"registry private /healthz HTTP {st}")

    # §8 schema proof on the database itself
    try:
        conn = db_connect()
        with conn, conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            heads = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
            tables = cur.fetchone()[0]
        conn.close()
        rep.record("S01", heads == [expected_head], f"alembic_version={heads}")
        rep.record("S02", tables >= 30, f"{tables} public tables")
    except Exception as e:
        rep.record("S01", False, f"database: {type(e).__name__}")

    # §9 staging operator (bootstrap allowlist) + a plain user for 403 checks
    password = derive_password(secret)
    op_token = ensure_user(rep, "O01", api, op_email, password)
    if op_token:
        st, body = http("GET", f"{api}/v1/society/config", token=op_token)
        rep.record("O02", st == 200, f"operator surface /v1/society/config HTTP {st}")
    user_token = ensure_user(rep, "U01", api, user_email, password)
    if user_token:
        st, body = http("GET", f"{api}/v1/society/config", token=user_token)
        rep.record("U02", st == 403, f"plain user refused on operator surface HTTP {st}")

    # §10 society smoke + red-team (scripted probes; the runtime flag is asserted
    # to match VALIDATOR_EXPECT_RUNTIME — "off" unless Phase 5 says otherwise)
    smoke = REPO / "deploy" / "society-staging-smoke.py"
    redteam = REPO / "deploy" / "society-staging-redteam.py"
    if op_token:
        rc, lines = run_child(smoke, ["--api", api, "--expect-runtime", expect_runtime, "--report", "/tmp/society-smoke.json"],
                              {"SOCIETY_SMOKE_TOKEN": op_token})
        for ln in lines:
            sys.stdout.write(f"  smoke: {ln}\n")
        rep.record("M01", rc == 0, f"society smoke exit {rc}")
        burst = env("REDTEAM_BURST", "40")
        rc, lines = run_child(redteam, ["--api", api, "--burst", burst, "--report", "/tmp/society-redteam.json"],
                              {"SOCIETY_REDTEAM_TOKEN": op_token, "SOCIETY_REDTEAM_USER_TOKEN": user_token or ""})
        for ln in lines:
            sys.stdout.write(f"  redteam: {ln}\n")
        rep.record("R01", rc == 0, f"society red-team exit {rc}")
    else:
        rep.record("M01", False, "no operator token")
        rep.record("R01", False, "no operator token")

    # core application smoke
    st, body = http("GET", f"{api}/v1/agents/public/")
    rep.record("C01", st == 200, f"public agent listing HTTP {st}")
    st, body = http("GET", f"{dash}/")
    rep.record("C02", st in (200, 302), f"dashboard / HTTP {st}")
    st, body = http("GET", f"{dash}/landing")
    rep.record("C03", st == 200, f"dashboard /landing HTTP {st}")
    st, body = http("GET", f"{dash}/metaverse")
    rep.record("C04", st == 200 and "Internal Server Error" not in body, f"dashboard /metaverse HTTP {st}")

    # §11 proxy-header spoof test (last: it exhausts this client's anonymous bucket for a minute)
    baseline, forged = spoof_test(api, int(env("SPOOF_BASELINE", "220")), int(env("SPOOF_FORGED", "60")))
    ok, detail = analyse_spoof(baseline, forged)
    rep.record("P01", ok, detail)
    return finish(rep)


def finish(rep: Report) -> int:
    if rep.failed:
        sys.stdout.write(f"VALIDATION RESULT: RED {','.join(rep.failed)} ({rep.count} checks)\n")
        return 1
    sys.stdout.write(f"VALIDATION RESULT: GREEN ({rep.count} checks)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
