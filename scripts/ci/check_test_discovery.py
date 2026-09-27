#!/usr/bin/env python3
"""Test-discovery sentinel: no repository-owned test suite may sit outside
required CI unnoticed (concept from PR #64, adopted by the Maintenance OS, ADR-0010).

A dashboard suite once lived in services/dashboard/tests/ while required CI
ran `pytest tests/`, so a production UI regression coexisted with a green
build. This script lists every repository-owned test file (test_*.py /
*_test.py), asks pytest which files the required CI roots collect, and fails
on any test file that is neither collected nor explicitly classified.

Classifications are explicit and small:

* CLASSIFIED -- deliberately never collected (fixture data, legacy, live-stack);
* HELD       -- trusted verification tests that are RED on the current product
                because of an OPEN maintenance incident. They are collected by
                required CI the moment the Society's repair makes them pass (PR
                #64 finalisation). A held file must be a contract verification
                test in services/registry/app/maintenance/desired_state.json or
                name the incident work it waits for; tests/test_test_discovery.py
                enforces that. HELD never weakens a test: the file is unchanged
                and still judges the repair (the Maintenance Kernel runs it).

    python scripts/ci/check_test_discovery.py
"""

from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: What required CI runs (.github/workflows/ci.yml "Full test suite").
REQUIRED_ROOTS = ("tests",)

#: Test files that are deliberately NOT collected, each with a reason.
CLASSIFIED = {
    "legacy/**": "archived code (docs/TEST_MATRIX.md 'Legacy'); not part of any active service",
    "tests/society/fixtures/**": "fixture repository DATA copied into throw-away git repos (tests/society/conftest.py collect_ignore)",
    "tests/test_integration.py": "needs a live registry (REGISTRY_URL): required CI --ignore's it explicitly and the fresh-install job drives the real stack",
}

#: Trusted verification tests held out of required CI while the incident they
#: judge is open. Remove an entry (and add its root to REQUIRED_ROOTS) when the
#: repair lands -- never by editing the test.
HELD = {
    "services/dashboard/tests/test_public_surface.py": "public_surface contract verification test; RED until the Society repairs the dashboard routing incident (templates url_for unregistered endpoints)",
    "services/dashboard/tests/test_experience_contract.py": "browser_experience contract verification test; RED until the Society repairs the experience-quality incident (contrast, raw capability dicts, error banner, dead links)",
    "services/dashboard/tests/test_main.py": "dashboard unit tests; 4 pre-existing failures (trust labels, 302 vs 200) belong to the same dashboard repair",
}

_PATTERNS = ("test_*.py", "*_test.py")


def repository_test_files(root: Path = ROOT) -> set:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=root, capture_output=True, text=True, check=True).stdout.split()
    return {p for p in out if any(fnmatch.fnmatch(Path(p).name, pat) for pat in _PATTERNS)}


def collected_files(roots, root: Path = ROOT) -> set:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *roots],
        cwd=root, capture_output=True, text=True,
    )
    if proc.returncode not in (0, 5):  # 5 = nothing collected in a root
        sys.stderr.write(proc.stdout[-4000:] + proc.stderr[-4000:])
        raise SystemExit("pytest --collect-only failed: a collection error is itself a test-discovery failure")
    files = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if "::" in line:
            files.add(line.split("::", 1)[0])
        elif ": " in line and line.rsplit(": ", 1)[-1].isdigit():
            files.add(line.rsplit(": ", 1)[0])
    return {f for f in files if f.endswith(".py")}


def classified(path: str) -> str:
    for pattern, reason in CLASSIFIED.items():
        if fnmatch.fnmatch(path, pattern):
            return reason
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--roots", nargs="*", default=list(REQUIRED_ROOTS))
    args = ap.parse_args(argv)
    every = repository_test_files()
    collected = collected_files(args.roots)
    orphans = sorted(p for p in every if p not in collected and not classified(p) and p not in HELD)
    print(f"TEST-DISCOVERY repository test files={len(every)} collected files={len(collected)} roots={list(args.roots)}")
    for p in sorted(every - collected):
        if classified(p):
            print(f"TEST-DISCOVERY classified {p}: {classified(p)}")
        elif p in HELD:
            print(f"TEST-DISCOVERY HELD {p}: {HELD[p]}")
    stale = sorted(p for p in HELD if p in collected)
    if stale:
        for p in stale:
            print(f"TEST-DISCOVERY STALE-HELD {p}: already collected; remove the HELD entry")
        return 1
    if orphans:
        for p in orphans:
            print(f"TEST-DISCOVERY ORPHAN {p}: not collected by required CI and not classified")
        return 1
    print("TEST-DISCOVERY PASS: every active test file is collected by required CI, classified, or HELD on an open incident")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
