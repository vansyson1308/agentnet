"""
``@pytest.mark.timeout`` must be REAL (Phase 2.6 §5.1).

Before this hardening the marker on the Society end-to-end test was silently
inert: pytest-timeout was not installed in CI and pytest only emitted a
``PytestUnknownMarkWarning``. Now:

* the plugin is part of ``requirements-dev.txt`` and must be loaded;
* ``pytest.ini`` carries the global ``timeout`` and turns unknown marks into
  errors, so a missing plugin fails collection instead of warning;
* an isolated pytest run proves a hanging test is actually killed by the
  marker (cheap: a 1-second timeout on a sleeping test).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

HANGING = textwrap.dedent(
    """
    import time

    import pytest


    @pytest.mark.timeout(1)
    def test_hangs():
        time.sleep(30)
    """
)


def test_pytest_timeout_plugin_is_loaded(pytestconfig):
    assert pytestconfig.pluginmanager.hasplugin("timeout"), "pytest-timeout must be installed (requirements-dev.txt)"
    assert float(pytestconfig.getini("timeout")) == 900.0, "global timeout comes from pytest.ini"


def _run(tmp_path, *extra):
    test_file = tmp_path / "test_hang.py"
    test_file.write_text(HANGING, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", "-q", str(test_file), *extra],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.timeout(120)
def test_timeout_marker_kills_a_hanging_test(tmp_path):
    result = _run(tmp_path)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Timeout" in result.stdout, result.stdout
    assert "1 failed" in result.stdout, result.stdout


@pytest.mark.timeout(120)
def test_without_the_plugin_the_marker_is_an_error_not_a_silent_no_op(tmp_path):
    """Exactly the failure mode this closes: with the plugin disabled the mark
    is unknown, and the warning policy turns that into a collection error
    rather than letting the test run unbounded."""
    result = _run(tmp_path, "-p", "no:timeout", "-W", "error::pytest.PytestUnknownMarkWarning")
    assert result.returncode != 0
    assert "PytestUnknownMarkWarning" in result.stdout + result.stderr
    assert "1 passed" not in result.stdout
