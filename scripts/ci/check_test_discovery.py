#!/usr/bin/env python3
"""Test-discovery sentinel: no repository-owned test suite may sit outside
required CI.

A dashboard suite once lived in services/dashboard/tests/ while required CI
ran `pytest tests/`, so a production UI regression coexisted with a green
build. This script lists every repository-owned test file (test_*.py /
*_test.py), asks pytest which files the required CI roots actually collect,
and fails on any test file that is neither collected nor explicitly
classified below with a reason.

    python scripts/ci/check_test_discovery.py            # uses REQUIRED_ROOTS
    python scripts/ci/check_test_discovery.py --roots tests services/dashboard/tests
"""

from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: What required CI runs (.github/workflows/ci.yml "Full test suite"). Keep in
#: sync: tests/test_test_discovery.py checks the workflow names these roots.
REQUIRED_ROOTS = ("tests", "services/dashboard/tests")

#: Test files that are deliberately NOT collected, each with a reason.
CLASSIFIED = {
    "legacy/**": "archived code (docs/TEST_MATRIX.md 'Legacy'); not part of any active service",
    "tests/society/fixtures/**": "fixture repository DATA copied into throw-away git repos (tests/society/conftest.py collect_ignore)",
    "tests/test_integration.py": "needs a live registry (REGISTRY_URL): required CI --ignore's it explicitly and the fresh-install job drives the real stack",
}

_PATTERNS = ("test_*.py", "*_test.py")


def repository_test_files(root: Path = ROOT) -> set:
    out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True).stdout.split()
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
        if "::" in line:  # node ids (-q)
            files.add(line.split("::", 1)[0])
        elif line.endswith(tuple(f": {n}" for n in range(0, 10))) or (": " in line and line.rsplit(": ", 1)[-1].isdigit()):
            files.add(line.rsplit(": ", 1)[0])  # "path: count" (-qq)
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
    orphans = sorted(p for p in every if p not in collected and not classified(p))
    by_root = {r: sum(1 for p in collected if p.startswith(r.rstrip("/") + "/")) for r in args.roots}
    print(f"TEST-DISCOVERY repository test files={len(every)} collected files={len(collected)} by root={by_root}")
    for p in sorted(every - collected):
        reason = classified(p)
        if reason:
            print(f"TEST-DISCOVERY classified {p}: {reason}")
    if orphans:
        for p in orphans:
            print(f"TEST-DISCOVERY ORPHAN {p}: not collected by required CI and not classified")
        return 1
    print("TEST-DISCOVERY PASS: every active test file is collected by required CI")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
