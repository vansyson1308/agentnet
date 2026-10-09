"""Bench judge: did an attempt make its task's tests pass?

Trusted and model-free. A task scores only when (1) every named failing test
passes -- none failed, errored or skipped -- and (2) its whole test files pass.
The judge (this file and tasks.json) is never edited in the same PR as the
harness it scores.

Tests run in the worktree with a scrubbed environment: no secrets, and no
database or Redis settings (the bench never touches a database).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from typing import Dict, List, Sequence

_SCRUB = re.compile(r"(PASSWORD|_KEY|SECRET|TOKEN|CREDENTIAL)", re.IGNORECASE)
_NO_DB = ("POSTGRES", "REDIS", "DATABASE_URL", "SOCIETY_TEST_DB")


def test_env(worktree: str) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not _SCRUB.search(k) and not k.startswith(_NO_DB)}
    env.update({"PYTHONPATH": worktree, "PYTHONDONTWRITEBYTECODE": "1", "ENVIRONMENT": "development",
                "POSTGRES_HOST": "127.0.0.1", "POSTGRES_PORT": "1", "SOCIETY_RUNTIME_ENABLED": "false"})
    return env


def run_pytest(worktree: str, targets: Sequence[str], *, timeout: int = 900) -> Dict[str, int]:
    """Counts from pytest's junit report: tests, failures, errors, skipped, rc."""
    xml = os.path.join(worktree, ".bench-junit.xml")
    if os.path.exists(xml):
        os.remove(xml)
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}", *targets]
    try:
        rc = subprocess.run(argv, cwd=worktree, env=test_env(worktree), capture_output=True, text=True, timeout=timeout, check=False).returncode
    except subprocess.TimeoutExpired:
        return {"tests": 0, "failures": 0, "errors": 1, "skipped": 0, "rc": -1}
    out = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "rc": rc}
    if os.path.exists(xml):
        for suite in ET.parse(xml).iter("testsuite"):
            for k in ("tests", "failures", "errors", "skipped"):
                out[k] += int(suite.get(k) or 0)
        os.remove(xml)
    return out


def score(worktree: str, task: dict, *, timeout: int = 900) -> dict:
    named: List[str] = list(task["failing_tests"])
    n = run_pytest(worktree, named, timeout=timeout)
    named_ok = n["rc"] == 0 and n["tests"] == len(named) and n["failures"] + n["errors"] + n["skipped"] == 0
    f = run_pytest(worktree, list(task["test_files"]), timeout=timeout) if named_ok else None
    files_ok = bool(f) and f["rc"] == 0 and f["tests"] > 0 and f["failures"] + f["errors"] == 0
    return {"passed": named_ok and files_ok, "named": n, "files": f}


__all__ = ["score", "run_pytest", "test_env"]
