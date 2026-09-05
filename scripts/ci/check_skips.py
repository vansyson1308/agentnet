#!/usr/bin/env python3
"""Fail CI when a pytest run contains skips whose reason is not on the
explicit allow list (Phase 2.5 §18: no silent exclusions).

Usage: check_skips.py REPORT.xml [--allow "substring" ...]

A skip is acceptable only when its reason contains one of the --allow
substrings. With no --allow, any skip fails.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--allow", action="append", default=[], help="allowed skip-reason substring (repeatable)")
    args = ap.parse_args(argv)

    tree = ET.parse(args.report)
    total = passed = failed = 0
    bad: list[str] = []
    allowed: list[str] = []
    for case in tree.iter("testcase"):
        total += 1
        skipped = case.find("skipped")
        if skipped is None:
            if case.find("failure") is not None or case.find("error") is not None:
                failed += 1
            else:
                passed += 1
            continue
        reason = (skipped.get("message") or skipped.text or "").strip()
        name = f"{case.get('classname')}::{case.get('name')}"
        if any(a in reason for a in args.allow):
            allowed.append(f"{name}: {reason}")
        else:
            bad.append(f"{name}: {reason or '<no reason>'}")

    sys.stdout.write(f"tests={total} passed={passed} failed={failed} skipped={len(allowed) + len(bad)}\n")
    for line in allowed:
        sys.stdout.write(f"  allowed skip: {line}\n")
    if bad:
        sys.stderr.write("UNEXPLAINED SKIPS (add a real reason on the allow list or make the test run):\n")
        for line in bad:
            sys.stderr.write(f"  {line}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
