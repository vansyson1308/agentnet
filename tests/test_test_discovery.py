"""The test-discovery sentinel (scripts/ci/check_test_discovery.py; PR #64's
concept adopted by ADR-0010): an active test suite can never again sit outside
required CI unnoticed, and a HELD suite is tied to the contract it judges."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _sentinel():
    spec = importlib.util.spec_from_file_location("check_test_discovery", ROOT / "scripts" / "ci" / "check_test_discovery.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_required_ci_runs_the_sentinel_and_its_roots():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    s = _sentinel()
    for root in s.REQUIRED_ROOTS:
        assert f"pytest {root}/" in ci, root
    assert "python scripts/ci/check_test_discovery.py" in ci


def test_held_files_are_contract_verification_tests_or_the_same_dashboard_repair():
    s = _sentinel()
    registry = json.loads((ROOT / "services/registry/app/maintenance/desired_state.json").read_text(encoding="utf-8"))
    verification = {t for c in registry["contracts"] for t in c["verification_tests"]}
    for path, reason in s.HELD.items():
        assert (ROOT / path).is_file(), path
        assert path in verification or "same dashboard repair" in reason, path
        assert "RED until" in reason or "pre-existing failures" in reason


def test_an_orphan_test_directory_is_detected(tmp_path):
    s = _sentinel()
    repo = tmp_path
    for rel, body in {
        "tests/test_ok.py": "def test_ok():\n    assert True\n",
        "services/new_ui/tests/test_page.py": "def test_page():\n    assert True\n",
        "legacy/old/test_archived.py": "def test_old():\n    assert True\n",
    }.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    every = s.repository_test_files(repo)
    collected = s.collected_files(["tests"], repo)
    orphans = sorted(p for p in every if p not in collected and not s.classified(p) and p not in s.HELD)
    assert orphans == ["services/new_ui/tests/test_page.py"]
    assert s.classified("legacy/old/test_archived.py")


def test_the_repository_has_no_orphan_test_suite():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "ci" / "check_test_discovery.py")], cwd=ROOT, capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert "TEST-DISCOVERY PASS" in proc.stdout
