"""Bench miner: turn merged work into CANDIDATE bench tasks (deterministic, no model).

    python scripts/bench/mine.py --repo <checkout> --since <sha> [--ref origin/main] > candidates.json

Per first-parent merge M after ``--since``, then per non-merge commit M (main's own
and those of merged PR branches): changed non-test, non-protected files =
files_allowed (1..5, all present at M^1); failing_tests = node ids of the changed test files that fail with
files_allowed reverted to M^1 and pass on M; task = the merged commits' messages
(a merge) or M's own message (80..900 chars). A candidate whose flipped tests an
existing task already judges is skipped. split = holdout when sha256(id) starts with 0-3.
It only PRINTS candidates: they land through an owner-merged PR, never with a harness change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Set

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from scripts.bench.score import test_env  # noqa: E402
from services.registry.app.society.engineering.workspace import is_protected  # noqa: E402  -- a path no builder may edit is never a task file

_TEST = re.compile(r"^tests/(.+/)?test_[^/]+\.py$")


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout.strip()


def outcomes(worktree: str, files: List[str], timeout: int = 900) -> Dict[str, bool]:
    """node id -> passed, from pytest's junit report."""
    xml = os.path.join(worktree, ".mine-junit.xml")
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}", *files], cwd=worktree, env=test_env(worktree),
                   capture_output=True, timeout=timeout, check=False)
    out: Dict[str, bool] = {}
    if os.path.exists(xml):
        for case in ET.parse(xml).iter("testcase"):
            parts = (case.get("classname") or "").split(".")
            cls = [p for p in parts if p[:1].isupper()]
            mod = "/".join(p for p in parts if p not in cls) + ".py"
            out["::".join([mod, *cls, case.get("name") or ""])] = not any(c.tag in ("failure", "error", "skipped") for c in case)
        os.remove(xml)
    return out


def task_text(repo: str, merge: str) -> str:
    merged = len(_git(repo, "rev-list", "--parents", "-n", "1", merge).split()) > 2
    subjects = _git(repo, "log", "--format=%s%n%b", *([f"{merge}^1..{merge}^2"] if merged else ["-1", merge])).splitlines()
    text = " ".join(ln.strip() for ln in subjects if ln.strip() and not ln.startswith(("Co-Authored-By", "Claude-Session", "Merge ", "diff ", "+++", "---", "@@")))
    return text if len(text) <= 900 else text[:900].rsplit(". ", 1)[0] + "."


def mine_one(repo: str, merge: str, root: str) -> Optional[dict]:
    try:
        changed = _git(repo, "diff", "--name-only", f"{merge}^1", merge).splitlines()
    except subprocess.CalledProcessError:  # a root commit: nothing to revert to
        return None
    tests = [f for f in changed if _TEST.match(f)]
    src = [f for f in changed if not f.startswith("tests/") and not f.startswith("scripts/bench/") and not is_protected(f)]
    text = task_text(repo, merge)
    if not tests or not 1 <= len(src) <= 5 or len(text) < 80:
        return None
    if any(subprocess.run(["git", "-C", repo, "cat-file", "-e", f"{merge}^1:{f}"], capture_output=True).returncode for f in src):
        return None  # a file the fix adds cannot be reverted by the bench's prepare()
    wt = os.path.join(root, merge[:12])
    _git(repo, "worktree", "add", "-q", "--detach", wt, merge)
    try:
        after = outcomes(wt, tests)
        _git(wt, "checkout", f"{merge}^1", "--", *src)
        before = outcomes(wt, tests)
    except subprocess.CalledProcessError:
        return None
    finally:
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", wt], capture_output=True, check=False)
    flipped = sorted(n for n, ok in after.items() if ok and before.get(n) is False)
    if not flipped:
        return None
    tid = re.sub(r"[^a-z0-9]+", "-", text.lower())[:40].strip("-")
    files = sorted({n.split("::")[0] for n in flipped})
    return {"id": tid, "fix_sha": merge, "files_allowed": src, "test_files": files, "failing_tests": flipped[:12], "task": text,
            "split": "holdout" if hashlib.sha256(tid.encode()).hexdigest()[0] in "0123" else "dev"}


def mine(repo: str, since: str, known: Set[str], ref: str = "origin/main", judged: Set[str] = frozenset()) -> List[dict]:
    merges = _git(repo, "rev-list", "--first-parent", "--merges", "--reverse", f"{since}..{ref}").split()
    singles = _git(repo, "rev-list", "--no-merges", "--reverse", f"{since}..{ref}").split()
    out, judged = [], set(judged)
    with tempfile.TemporaryDirectory(prefix="bench-mine-") as root:
        for m in merges + singles:
            if m in known:
                continue
            cand = mine_one(repo, m, root)
            if cand is not None and not judged & set(cand["failing_tests"]):
                judged |= set(cand["failing_tests"])
                out.append(cand)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--since", required=True)
    ap.add_argument("--ref", default="origin/main")
    args = ap.parse_args(argv)
    here = os.path.dirname(os.path.abspath(__file__))
    tasks = [t for f in ("tasks.json", "holdout.json") if os.path.exists(os.path.join(here, f)) for t in json.load(open(os.path.join(here, f)))["tasks"]]
    known, judged = {t["fix_sha"] for t in tasks}, {n for t in tasks for n in t["failing_tests"]}
    print(json.dumps({"candidates": mine(args.repo, args.since, known, args.ref, judged)}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
