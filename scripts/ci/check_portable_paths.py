#!/usr/bin/env python3
"""Fail CI when a tracked path cannot be checked out on Windows.

NTFS refuses the characters  : " < > | ? *  in a file or directory name, so a
single such path makes `git clone` fail on Windows (2026-10-08: twelve
legacy Hermes notes under services/registry/app/features/ had ':' in their
names). Paths are read NUL-separated, so quoting never hides one.

Usage: check_portable_paths.py   (run from the repository root)
"""

from __future__ import annotations

import subprocess
import sys

FORBIDDEN = frozenset(':"<>|?*')


def offending(paths):
    return [p for p in paths if FORBIDDEN & set(p)]


def tracked_paths():
    out = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True).stdout
    return [p for p in out.decode("utf-8", errors="surrogateescape").split("\0") if p]


def main() -> int:
    bad = offending(tracked_paths())
    for p in bad:
        print(f"PORTABLE-PATH FAIL {p!r} contains {''.join(sorted(FORBIDDEN & set(p)))!r}")
    print(f"PORTABLE-PATH {'FAIL' if bad else 'PASS'}: {len(bad)} tracked path(s) with any of : \" < > | ? *")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
