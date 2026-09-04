"""Fresh-install / restart / upgrade proof (process based).

Runs only when AGENTNET_FRESH_INSTALL=1 (it creates databases, starts a private Redis and four
service processes, and takes minutes). CI runs it in the dedicated `fresh-install` job; locally:

    AGENTNET_FRESH_INSTALL=1 pytest tests/test_fresh_install.py -q
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "fresh_install" / "run_fresh_install.py"

pytestmark = pytest.mark.skipif(
    os.getenv("AGENTNET_FRESH_INSTALL") != "1",
    reason="fresh-install harness: set AGENTNET_FRESH_INSTALL=1 (needs PostgreSQL, redis-server, free ports; run by the CI fresh-install job)",
)


@pytest.mark.parametrize("snapshot", ["none", "pre-society"])
def test_fresh_install_and_restart(snapshot, tmp_path):
    if shutil.which("redis-server") is None:
        pytest.fail("redis-server binary is required for the fresh-install harness")
    # AGENTNET_FRESH_INSTALL_REPORT_DIR lets CI keep the JSON report + service
    # logs as an artifact; locally they land in pytest's tmp_path.
    out = pathlib.Path(os.getenv("AGENTNET_FRESH_INSTALL_REPORT_DIR") or tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    report = out / f"fresh-{snapshot}.json"
    proc = subprocess.run([sys.executable, str(HARNESS), "--snapshot", snapshot, "--report", str(report)], cwd=ROOT, capture_output=True, text=True, timeout=1800)
    sys.stdout.write(proc.stdout[-4000:])
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-1000:]
