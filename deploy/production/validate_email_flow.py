#!/usr/bin/env python3
"""AgentNet — production email and account-flow validation.

Phase 7 could prove only the REFUSAL: with `EMAIL_DELIVERY_PROVIDER=disabled`
registration answered 503 and created nothing. That was the honest contract at
the time, but it meant every authenticated production path was unproven,
because every one of them needs a logged-in user and no user could be created.

This validator proves the other half, once real SMTP delivery is configured:

    register -> verification email -> verify -> login -> authenticated reads

It runs INSIDE the production private network (the `prod-validator` service),
because the production registry has no public domain and must not get one to
make a test convenient.

What it never does
------------------
* It never prints the verification token, the canary password or the JWT.
  The token is a credential: whoever holds it can activate the account.
* It never writes to the database. The one SELECT it makes is a read of the
  token the application itself generated, and it exists only because the
  validator cannot read the mailbox the message was delivered to.

Why reading the token from the database is not a bypass
-------------------------------------------------------
The token is CONSUMED through the real public endpoint, exactly as a human
clicking the link would consume it. The database read answers "which token",
not "may I skip verification".

That the DELIVERED message carries the SAME token is proven out of band: this
validator prints `token.fingerprint` -- the first 16 hex characters of the
token's SHA-256 -- and the operator compares it with the fingerprint of the
link in the message Resend recorded. A truncated hash of a 256-bit random
token identifies it without revealing it, so the comparison can be published
while the token cannot.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import uuid
from typing import Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from validate import Report, _http, labelled_sink  # noqa: E402  (deliberate: same directory)

#: Resend's simulated-delivery address. It accepts and records a real send
#: without a human inbox, which is what a production canary should use.
DEFAULT_CANARY_EMAIL = "delivered@resend.dev"


def fresh_canary_email() -> str:
    """A NEW sink address per run: `delivered+prod-email-<hex>@resend.dev`.

    An address registers once. The first live run used the bare sink, so the
    same default on any later run would answer "already registered" at E01 and
    report a production failure that is the validator's own leftover.
    """
    return labelled_sink(DEFAULT_CANARY_EMAIL, f"prod-email-{uuid.uuid4().hex[:10]}")

#: Registration blocks on SMTP delivery, so it needs a timeout longer than the
#: registry's own (``SMTP_TIMEOUT_SECONDS``, 15s by default) multiplied by the
#: address families a connect can try.
REGISTRATION_TIMEOUT_SECONDS = 90.0


def canary_password() -> str:
    """A fresh password per run, satisfying the registry's policy.

    Assembled from a uuid rather than written as a literal: a password-shaped
    string in a tracked file is a credential in git, even for a canary, and
    this repository's own secret scan is right to refuse it.
    """
    return "Pc" + uuid.uuid4().hex[:14].capitalize() + "1!"


def fingerprint(secret: str) -> str:
    """Publishable identity of a secret: first 16 hex of its SHA-256."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def read_verification_token(email: str) -> Tuple[Optional[str], str]:
    """Read the newest unconsumed verification token for `email`.

    Returns (token, detail). The token is returned for USE, never for display;
    every caller must treat it the way it treats a password.
    """
    try:
        import psycopg2  # imported here so the module imports without a driver
    except ImportError:  # pragma: no cover - production image always has it
        return None, "psycopg2 unavailable"

    dsn = dict(
        host=os.getenv("POSTGRES_HOST", ""),
        port=int(os.getenv("POSTGRES_PORT", "5432") or "5432"),
        dbname=os.getenv("POSTGRES_DB", ""),
        user=os.getenv("POSTGRES_USER", ""),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        connect_timeout=10,
    )
    if not dsn["host"] or not dsn["dbname"]:
        return None, "POSTGRES_* not configured on this service"
    try:
        conn = psycopg2.connect(**dsn)
    except Exception as exc:  # noqa: BLE001 - the DSN must never reach the log
        return None, f"connect failed ({type(exc).__name__})"
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.token
                  FROM email_verification_tokens t
                  JOIN users u ON u.id = t.user_id
                 WHERE u.email = %s AND t.consumed_at IS NULL
                 ORDER BY t.created_at DESC
                 LIMIT 1
                """,
                (email,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None, "no unconsumed verification token for the canary address"
    return row[0], "one unconsumed token found"


def check_email_flow(report: Report, registry: str, payment: str, email: str) -> None:
    password = canary_password()
    report.record("email_flow.canary_email", email)

    # ── E01 registration must now SUCCEED, where it used to answer 503 ──
    #
    # Registration attempts SMTP delivery BEFORE it commits, so this one call
    # can take as long as the registry's SMTP timeout plus connect attempts to
    # every address family. The default 20s timeout was shorter than that path:
    # the first live run reported status 0 (the VALIDATOR's own timeout) for
    # what was really a delivery failure, which hides the status code that
    # would have named the problem. A validator must outlast what it measures.
    status, _ = _http("POST", f"{registry}/v1/auth/user/register",
                      body={"email": email, "password": password},
                      timeout=REGISTRATION_TIMEOUT_SECONDS)
    report.check("email_flow", "E01", status in (200, 201),
                 f"registration through the normal API -> {status} "
                 "(201 means SMTP accepted the verification message: delivery "
                 "is attempted BEFORE the account is committed)")
    if status not in (200, 201):
        # Everything below needs the account that was not created. Stopping is
        # the honest outcome; continuing would report cascading failures that
        # all have one cause.
        report.check("email_flow", "E02", False, "skipped: no account was created")
        return

    # ── E02 an unverified account must NOT be able to log in ──
    status, _ = _http("POST", f"{registry}/v1/auth/user/login",
                      body={"email": email, "password": password})
    report.check("email_flow", "E02", status == 403,
                 f"login BEFORE verification -> {status} (expected 403: the "
                 "link is what activates the account, not registration)")

    # ── E03 the token the application generated ──
    token, detail = read_verification_token(email)
    report.check("email_flow", "E03", bool(token), detail)
    if not token:
        return
    report.record("email_flow.token_fingerprint", fingerprint(token))

    # ── E04 the real endpoint accepts it ──
    status, _ = _http("GET", f"{registry}/v1/auth/verify-email?token={token}")
    report.check("email_flow", "E04", status == 200,
                 f"verify-email with the delivered token -> {status}")

    # ── E05 and refuses it a second time ──
    status, _ = _http("GET", f"{registry}/v1/auth/verify-email?token={token}")
    report.check("email_flow", "E05", status == 400,
                 f"replay of the SAME token -> {status} (expected 400: "
                 "single-use, so an intercepted link cannot be reused)")

    # ── E06 the verified account can log in ──
    status, payload = _http("POST", f"{registry}/v1/auth/user/login",
                            body={"email": email, "password": password})
    jwt = (payload or {}).get("access_token", "") if isinstance(payload, dict) else ""
    report.check("email_flow", "E06", status == 200 and bool(jwt),
                 f"login AFTER verification -> {status} "
                 f"(access token {'issued' if jwt else 'MISSING'}; never printed)")
    if not jwt:
        return

    # ── E07-E10: the authenticated smoke Phase 7 could not run ──
    status, payload = _http("GET", f"{registry}/v1/tasks/", token=jwt)
    empty = isinstance(payload, list) and payload == []
    report.check("email_flow", "E07", status == 200 and empty,
                 f"authenticated task list -> {status} "
                 f"({'empty, as a new account must be' if empty else 'NOT empty'})")

    status, _ = _http("GET", f"{registry}/v1/tasks/{uuid.uuid4()}", token=jwt)
    report.check("email_flow", "E08", status == 404,
                 f"someone else's task id with a valid token -> {status} "
                 "(expected 404: authentication is not authorisation)")

    if not payment:
        report.check("email_flow", "E09", True,
                     "wallet checks skipped: PROD_PAYMENT_URL not configured")
        return

    status, payload = _http("GET", f"{payment}/v1/wallets/", token=jwt)
    wallets = payload if isinstance(payload, list) else []
    one_empty_user_wallet = (
        len(wallets) == 1
        and wallets[0].get("owner_type") == "user"
        and wallets[0].get("balance_credits") == 0
        and float(wallets[0].get("balance_usdc") or 0) == 0.0
        and wallets[0].get("reserved_credits") == 0
    )
    report.check("email_flow", "E09", status == 200 and one_empty_user_wallet,
                 f"owner reads own wallet -> {status}; exactly one user wallet "
                 f"at zero balance with nothing reserved: {one_empty_user_wallet}")

    status, _ = _http("GET", f"{payment}/v1/wallets/{uuid.uuid4()}/balance", token=jwt)
    report.check("email_flow", "E10", status in (403, 404),
                 f"another wallet's balance with a valid token -> {status} "
                 "(expected 403/404, never 200)")

    status, _ = _http("GET", f"{payment}/v1/wallets/")
    report.check("email_flow", "E11", status in (401, 403),
                 f"anonymous wallet list -> {status} (expected 401/403)")


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="AgentNet production email/account flow validation")
    ap.add_argument("--registry", default=os.getenv("PROD_REGISTRY_URL", ""))
    ap.add_argument("--payment", default=os.getenv("PROD_PAYMENT_URL", ""))
    ap.add_argument("--email", default=os.getenv("CANARY_EMAIL", "").strip() or None)
    args = ap.parse_args(argv)
    if not args.email:
        args.email = fresh_canary_email()

    if not args.registry:
        print("PROD-EMAIL RESULT: FAILED (no registry URL)")
        return 2

    report = Report()
    started = time.time()
    report.record("email_flow.validated_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    report.record("email_flow.release_sha", os.getenv("RAILWAY_GIT_COMMIT_SHA", "unknown"))

    check_email_flow(report, args.registry.rstrip("/"), args.payment.rstrip("/"), args.email)
    report.record("email_flow.elapsed_seconds", round(time.time() - started, 2))

    if report.failed:
        print(f"PROD-EMAIL RESULT: FAILED {','.join(report.failed)} ({len(report.checks)} checks)")
        return 1
    print(f"PROD-EMAIL RESULT: OK ({len(report.checks)} checks)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
