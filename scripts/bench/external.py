"""Build the EXTERNAL holdout: a pinned subset of SWE-bench Verified (deterministic, no model).

    python scripts/bench/external.py --rows verified.json --repo sympy/sympy --min-version 1.5 --limit 24 > holdout_external.json

``--rows``: the dataset rows (``princeton-nlp/SWE-bench_Verified``, test split) as a JSON
list. Per instance of ``--repo`` (sorted by sha256(instance_id)): 1..3 changed source
files, FAIL_TO_PASS names found once each in the test patch's test files. It is kept
only when THIS judge (score.py, in this interpreter -- run it with the validator
image's Python) scores it failing on base + test patch and passing once the gold
patch is applied. The gold patch is never written out.
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
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from scripts.bench import run  # noqa: E402
from scripts.bench.score import score  # noqa: E402

_FILE = re.compile(r"^diff --git a/(\S+) b/", re.M)


def candidate(row: dict, deps: List[str]) -> Optional[dict]:
    src = [f for f in _FILE.findall(row["patch"]) if "/tests/" not in f]
    test_files = [f for f in _FILE.findall(row["test_patch"]) if re.search(r"/tests/test_[^/]+\.py$", f)]
    text = row["problem_statement"].strip()
    text = text if len(text) <= 4000 else text[:4000].rsplit("\n\n", 1)[0]
    if not 1 <= len(src) <= 3 or not test_files or len(text) < 80:
        return None
    return {"id": row["instance_id"], "repo": f"https://github.com/{row['repo']}", "base_sha": row["base_commit"], "test_patch": row["test_patch"],
            "deps": deps, "files_allowed": src, "test_files": test_files, "failing_tests": json.loads(row["FAIL_TO_PASS"]), "task": text}


def verify(task: dict, gold: str, root: str) -> Optional[dict]:
    try:
        ws = run.prepare("", task, root)
        names = []
        for n in task["failing_tests"]:
            hits = [f for f in task["test_files"] if re.search(rf"^def {re.escape(n)}\(", (ws.path / f).read_text(encoding="utf-8"), re.M)]
            if len(hits) != 1:
                return None
            names.append(f"{hits[0]}::{n}")
        task = {**task, "failing_tests": names}
        if score(str(ws.path), task)["named"]["rc"] == 0:
            return None  # the bug is not visible on the base
        subprocess.run(["git", "apply", "-"], cwd=ws.path, input=gold, text=True, check=True, capture_output=True)
        return task if score(str(ws.path), task)["passed"] else None
    except (subprocess.CalledProcessError, OSError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rows", required=True)
    ap.add_argument("--repo", default="sympy/sympy")
    ap.add_argument("--min-version", default="1.5")
    ap.add_argument("--deps", default="mpmath==1.3.0")
    ap.add_argument("--limit", type=int, default=24)
    args = ap.parse_args(argv)
    floor = tuple(map(int, args.min_version.split(".")))
    rows = [r for r in json.load(open(args.rows, encoding="utf-8")) if r["repo"] == args.repo and tuple(map(int, r["version"].split("."))) >= floor]
    rows.sort(key=lambda r: hashlib.sha256(r["instance_id"].encode()).hexdigest())
    deps = [d for d in args.deps.split(",") if d]
    run.ensure_deps([{"deps": deps}])
    out: List[dict] = []
    with tempfile.TemporaryDirectory(prefix="bench-external-") as root:
        for r in rows:
            task = candidate(r, deps)
            task = task and verify(task, r["patch"], root)
            if task:
                out.append(task)
                print(f"verified {task['id']}", file=sys.stderr, flush=True)
            if len(out) >= args.limit:
                break
    about = "AgentNet builder bench EXTERNAL HOLDOUT: a pinned SWE-bench Verified subset (scripts/bench/external.py). Never in any model context."
    print('{"version": 1, "about": ' + json.dumps(about) + ', "tasks": [\n' + ",\n".join(json.dumps(t) for t in out) + "\n]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
