#!/usr/bin/env python3
"""AgentNet -- public-surface validator (local / staging / production).

Checks the public product against the ONE public-surface contract
(services/registry/app/society/public_surface_contract.json) with the same
deterministic code the Society's synthetic monitor and CI use
(services/registry/app/society/surface.py). Anonymous HTTP only: it needs no
credential, reads nothing private and writes nothing.

    python deploy/public_surface_validate.py --env production
    python deploy/public_surface_validate.py --env staging
    python deploy/public_surface_validate.py --ui http://localhost:8080 --api http://localhost:8000

Output: one ``SURFACE <result> <severity> <name> ...`` line per observation and
one ``SURFACE-JSON {...}`` line with the structural report. Response bodies are
never printed. Exit 0 when no critical or major item fails (minor findings are
reported, not fatal), else 1; exit 2 on a contract or usage error.

It reports and never repairs.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SURFACE_PY = ROOT / "services" / "registry" / "app" / "society" / "surface.py"

#: Staging has no custom domain; these are its Railway service domains.
PRESETS = {
    "staging": {
        "ui": "https://dashboard-staging-4767.up.railway.app",
        "api": "https://registry-staging-145d.up.railway.app",
    },
}


def load_surface():
    """Import surface.py by path: it depends only on httpx and the stdlib,
    so the validator does not need the registry's database settings."""
    spec = importlib.util.spec_from_file_location("agentnet_public_surface", SURFACE_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", choices=["production", "staging", "local"], default=None)
    ap.add_argument("--ui", help="UI origin (overrides --env)")
    ap.add_argument("--api", help="API origin (overrides --env)")
    ap.add_argument("--no-crawl", action="store_true", help="contract items only; skip links/forms/assets")
    ap.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args(argv)

    surface = load_surface()
    try:
        contract = surface.load_contract()
    except surface.ContractError as exc:
        print(f"SURFACE-ERROR contract: {exc}")
        return 2
    origins = dict(contract.origins)
    if args.env == "staging":
        origins = dict(PRESETS["staging"])
    elif args.env == "local":
        origins = {"ui": "http://localhost:8080", "api": "http://localhost:8000"}
    if args.ui:
        origins["ui"] = args.ui
    if args.api:
        origins["api"] = args.api
    for key, value in origins.items():
        if not value.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            print(f"SURFACE-ERROR origin {key} must be https (or local http)")
            return 2

    report = surface.run_contract(origins, contract=contract, crawl=not args.no_crawl, timeout=args.timeout)
    for o in report.observations:
        result = "PASS" if o.ok else "FAIL"
        extra = ""
        if not o.ok:
            extra = f" failure={o.failure}"
            if o.markers_missing:
                extra += f" markers_missing={list(o.markers_missing)}"
            if o.detail:
                extra += f" ({o.detail})"
        print(
            f"SURFACE {result} {o.severity} {o.name} initial={o.initial_status} final={o.final_status} "
            f"final_path={o.final_path} latency_ms={o.latency_ms}{extra}"
        )
    blocking = report.failures(min_severity="major")
    print("SURFACE-JSON " + json.dumps(report.to_dict(), sort_keys=True))
    print(f"SURFACE RESULT {'FAIL' if blocking else 'PASS'} ({len(report.observations)} observations, {len(blocking)} blocking)")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
