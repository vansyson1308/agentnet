#!/usr/bin/env python3
"""Deep-tier browser probe (ADR-0010 D10): deterministic, no model, no credentials.

    # the real dashboard with stable fixture data (visual-test mode)
    python deploy/maintenance/browser_probe.py --local-dashboard

    # a deployed origin (staging / production), structural JSON to stdout
    python deploy/maintenance/browser_probe.py --origin https://agentnet.io.vn --target production

    # ...and hand the STRUCTURAL result to the Maintenance OS ingress
    MAINTENANCE_INGEST_URL=https://api.agentnet.io.vn/v1/maintenance/observations/browser \\
    MAINTENANCE_INGEST_TOKEN=<event-producer user JWT> \\
    python deploy/maintenance/browser_probe.py --origin https://agentnet.io.vn --target production --post

``--axe PATH`` injects a pinned axe-core build (npm ``axe-core@4.10.3``). Page
text never leaves the browser; the output is rule ids, counts, selector
classes, safe paths and numbers. Exit code 0 = every page passed, 1 = findings.
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
    if args.post:
        import httpx

        url, tok = os.getenv("MAINTENANCE_INGEST_URL", ""), os.getenv("MAINTENANCE_INGEST_TOKEN", "")
        if not (url and tok):
            print("post skipped: MAINTENANCE_INGEST_URL / MAINTENANCE_INGEST_TOKEN not set", file=sys.stderr)
        else:
            r = httpx.post(url, json=report, headers={"Authorization": f"Bearer {tok}"}, timeout=30)
            print(f"ingest: HTTP {r.status_code}", file=sys.stderr)
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
