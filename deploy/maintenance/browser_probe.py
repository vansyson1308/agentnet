#!/usr/bin/env python3
"""Deep-tier browser probe (ADR-0010 D10): deterministic, no model, no credentials.

    # the real dashboard with stable fixture data (visual-test mode)
    python deploy/maintenance/browser_probe.py --local-dashboard

    # a deployed origin (staging / production), structural JSON to stdout
    python deploy/maintenance/browser_probe.py --origin https://agentnet.io.vn --target production

    # ...and hand the STRUCTURAL result to the Maintenance OS ingress (the
    # registry that runs the kernel: staging, where the control plane lives)
    MAINTENANCE_INGEST_URL=https://<staging registry>/v1/maintenance/observations/browser \\
    MAINTENANCE_INGEST_EMAIL=<event-producer account> MAINTENANCE_INGEST_PASSWORD=<its password> \\
    python deploy/maintenance/browser_probe.py --origin https://agentnet.io.vn --target production --post

The probe logs in once per run (``/v1/auth/user/login`` on the ingest URL's
origin) and posts with that fresh user JWT: user JWTs expire after
``JWT_EXPIRATION``, so a stored token would silently stop a scheduled job.
``MAINTENANCE_INGEST_TOKEN`` (an already-issued JWT) is still accepted for a
one-off manual run.

``--axe PATH`` injects a pinned axe-core build (npm ``axe-core@4.10.3``). Page
text never leaves the browser; the output is rule ids, counts, selector
classes, safe paths and numbers. Exit code 0 = every page passed, 1 = findings,
3 = the ingress was configured but the report was not accepted (login or post
failed): a broken monitor, never a finding.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services" / "registry"))

from app.maintenance.browser import run_journeys  # noqa: E402
from app.maintenance.contracts import load_registry  # noqa: E402

from deploy.maintenance.dashboard_fixture import CRITICAL_PAGES, serve_dashboard  # noqa: E402

INGEST_BROKEN = 3


def ingest_login_url(ingest_url: str) -> str:
    """The user-login endpoint on the ingest URL's own origin (never another host)."""
    from urllib.parse import urlsplit

    parts = urlsplit(ingest_url)
    if parts.scheme not in ("https", "http") or not parts.netloc:
        raise ValueError("MAINTENANCE_INGEST_URL must be an absolute http(s) URL")
    return f"{parts.scheme}://{parts.netloc}/v1/auth/user/login"


def ingest_token(http, ingest_url: str, env=os.environ) -> tuple:
    """(token, status line). A configured JWT wins; otherwise log in with the
    event-producer account. Never returns or prints a credential in the status."""
    tok = env.get("MAINTENANCE_INGEST_TOKEN", "")
    if tok:
        return tok, "static token"
    email, password = env.get("MAINTENANCE_INGEST_EMAIL", ""), env.get("MAINTENANCE_INGEST_PASSWORD", "")
    if not (email and password):
        return "", "no ingest credential configured"
    r = http.post(ingest_login_url(ingest_url), json={"email": email, "password": password}, timeout=30)
    if r.status_code != 200:
        return "", f"ingest login: HTTP {r.status_code}"
    try:
        return r.json().get("access_token") or "", "ingest login: HTTP 200"
    except ValueError:
        return "", "ingest login: unreadable response"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--origin")
    ap.add_argument("--local-dashboard", action="store_true")
    ap.add_argument("--target", default="staging", choices=("production", "staging", "preview"))
    ap.add_argument("--axe", default=os.getenv("MAINTENANCE_AXE_SOURCE", ""))
    ap.add_argument("--no-links", action="store_true")
    ap.add_argument("--post", action="store_true")
    args = ap.parse_args(argv)
    axe = pathlib.Path(args.axe).read_text(encoding="utf-8") if args.axe else None
    budgets = load_registry().get("performance_budgets").budgets
    if args.local_dashboard:
        with serve_dashboard() as base:
            results = run_journeys(base, CRITICAL_PAGES, axe_source=axe, budgets=budgets, check_links=not args.no_links)
    elif args.origin:
        results = run_journeys(args.origin, CRITICAL_PAGES, axe_source=axe, budgets=budgets, check_links=not args.no_links, visual_mode=False)
    else:
        ap.error("--origin or --local-dashboard is required")
    report = {"target": args.target, "collector_version": "browser/1", "pages": [{k: v for k, v in r.as_dict().items() if k != "ok"} for r in results]}
    print(json.dumps(report, indent=2, sort_keys=True))
    findings = 0 if all(r.ok for r in results) else 1
    if args.post:
        import httpx

        url = os.getenv("MAINTENANCE_INGEST_URL", "")
        has_credential = any(os.getenv(n) for n in ("MAINTENANCE_INGEST_TOKEN", "MAINTENANCE_INGEST_EMAIL", "MAINTENANCE_INGEST_PASSWORD"))
        if not url and not has_credential:
            print("post skipped: the Maintenance OS ingress is not configured", file=sys.stderr)
            return findings
        tok, how = ingest_token(httpx, url, os.environ) if url else ("", "MAINTENANCE_INGEST_URL not set")
        print(how, file=sys.stderr)
        if not tok:
            return INGEST_BROKEN
        r = httpx.post(url, json=report, headers={"Authorization": f"Bearer {tok}"}, timeout=30)
        print(f"ingest: HTTP {r.status_code}", file=sys.stderr)
        if r.status_code not in (200, 201, 202):
            return INGEST_BROKEN
    return findings


if __name__ == "__main__":
    raise SystemExit(main())
