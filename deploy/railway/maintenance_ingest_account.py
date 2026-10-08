#!/usr/bin/env python3
"""Provision the scheduled browser probe's ingest account on staging.

A ``staging-validator`` program (``VALIDATOR_SCRIPT=maintenance_ingest_account.py``).
The deep-tier browser probe (``.github/workflows/maintenance-browser-probe.yml``)
logs in once per run as an ``event_producer`` user and posts its STRUCTURAL
report to the staging registry's ``/v1/maintenance/observations/browser``.

The owner chooses that account's email and password and stores them twice,
never in chat: as the GitHub repository secrets MAINTENANCE_INGEST_EMAIL /
MAINTENANCE_INGEST_PASSWORD, and on this Railway service as
MAINT_INGEST_EMAIL / MAINT_INGEST_PASSWORD. This program then:

1. registers the account on the staging registry (or reuses it) and marks its
   email verified on the staging database -- the staging practice, SMTP is
   not wired (validate_staging.ensure_user);
2. logs in as the validator operator and grants ``event_producer`` through
   the operator API (``POST /v1/society/operators``, the one role authority);
3. proves the account can log in and holds exactly ``event_producer``.

It never prints the password or a token and posts no observation (no
synthetic incident is ever created).

    MAINT-INGEST <code> PASS|FAIL <detail>
    MAINT-INGEST RESULT: OK | FAILED <codes>
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import validate_staging as vs  # noqa: E402


def main() -> int:
    api = vs.env("REGISTRY_PUBLIC_URL").rstrip("/")
    email = vs.env("MAINT_INGEST_EMAIL").lower()
    password = os.getenv("MAINT_INGEST_PASSWORD", "")
    secret = vs.env("STAGING_VALIDATOR_SECRET") or vs.env("VALIDATOR_SECRET")
    operator_email = vs.env("VALIDATOR_OPERATOR_EMAIL")
    failed = []

    def record(code: str, ok: bool, detail: str) -> None:
        if not ok:
            failed.append(code)
        print(f"MAINT-INGEST {code} {'PASS' if ok else 'FAIL'} {detail}", flush=True)

    if not (api and email and len(password) >= 12 and len(secret) >= 32 and operator_email):
        record("G00", False, "REGISTRY_PUBLIC_URL, MAINT_INGEST_EMAIL, MAINT_INGEST_PASSWORD (>= 12 chars), the validator secret and VALIDATOR_OPERATOR_EMAIL are required")
        return _finish(failed)
    if email == operator_email.lower():
        record("G01", False, "the ingest account must not be the operator account (least privilege)")
        return _finish(failed)
    rep = vs.Report()
    token = vs.ensure_user(rep, "G10", api, email, password)
    record("G10", bool(token), f"ingest account {email.split('@')[0]} registered/verified/logged in")
    if not token:
        return _finish(failed)
    op = vs.ensure_user(rep, "G20", api, operator_email, vs.derive_password(secret))
    record("G20", bool(op), "validator operator logged in")
    if not op:
        return _finish(failed)
    st, body = vs.http("POST", f"{api}/v1/society/operators", body={"email": email, "role": "event_producer"}, token=op)
    role = json.loads(body).get("role") if st == 200 else None
    record("G30", st == 200 and role == "event_producer", f"role grant via the operator API: HTTP {st} role={role}")
    st, body = vs.http("GET", f"{api}/v1/society/operators", token=op)
    rows = json.loads(body) if st == 200 else []
    mine = [r for r in rows if isinstance(r, dict) and (r.get("email") or "").lower() == email]
    record("G40", len(mine) == 1 and mine[0].get("role") == "event_producer", f"operator listing shows the account with role={mine[0].get('role') if mine else None}")
    st, _ = vs.http("POST", f"{api}/v1/auth/user/login", body={"email": email, "password": password})
    record("G50", st == 200, f"ingest account can log in: HTTP {st}")
    return _finish(failed)


def _finish(failed) -> int:
    print(f"MAINT-INGEST RESULT: {'OK' if not failed else 'FAILED ' + ','.join(failed)}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
