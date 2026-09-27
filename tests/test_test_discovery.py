"""The test-discovery sentinel (scripts/ci/check_test_discovery.py): an
active test suite can never again sit outside required CI unnoticed, the
way services/dashboard/tests/ did while a production UI regression shipped."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _sentinel():
    spec = importlib.util.spec_from_file_location("check_test_discovery", ROOT / "scripts" / "ci" / "check_test_discovery.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_required_ci_runs_every_root_the_sentinel_expects():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    s = _sentinel()
    assert "pytest tests/ services/dashboard/tests/" in ci
    for root in s.REQUIRED_ROOTS:
        assert f"{root}/" in ci, root
    assert "python scripts/ci/check_test_discovery.py" in ci


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
    orphans = sorted(p for p in every if p not in collected and not s.classified(p))
    assert orphans == ["services/new_ui/tests/test_page.py"]
    assert s.classified("legacy/old/test_archived.py")


def test_the_repository_has_no_orphan_test_suite():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "ci" / "check_test_discovery.py")], cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert "TEST-DISCOVERY PASS" in proc.stdout
