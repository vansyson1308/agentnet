"""QA proves an Architect test_first judges the change: RED on the candidate base, GREEN on the head.

Owner 2026-10-27: a test_first is pinned, committed before the build and exempt from no_self_judging
-- so a trivial one (``assert True``, or one the base already satisfies) could judge anything. The
pre-build check (executor._test_first_green) only looked once, before the first build; QA now proves
both sides on every verdict: the pinned tests fail on the base revision with nothing but themselves
added, and pass on the candidate head."""

from __future__ import annotations

from services.registry.app.society import spec_gate
from services.registry.app.society.engineering import qa
from services.registry.app.society.engineering import workspace as ws_mod

from .test_build_engine import TESTS

NEW_TEST = "tests/test_parse_bool_on.py"
FAILS_ON_BASE = "from app.textutil import parse_bool\n\n\ndef test_on_is_true():\n    assert parse_bool('on') is True\n"
TRIVIAL = "def test_anything():\n    assert True\n"
NEVER_GREEN = "from app.textutil import parse_bool\n\n\ndef test_maybe_is_true():\n    assert parse_bool('maybe') is True\n"
SRC = "app/textutil.py"


def _qa(settings, content, *, acceptance=(*TESTS, NEW_TEST)):
    import uuid

    ws = ws_mod.ensure_workspace(settings, uuid.uuid4())
    pinned = spec_gate.pin({"test_first": [{"path": NEW_TEST, "content": content}]})
    spec_gate.commit_test_first(ws, pinned)
    src = ws.path / SRC
    src.write_text(src.read_text().replace('_TRUE = {"true", "1"}', '_TRUE = {"true", "1", "yes", "on", "y"}'))
    ws_mod.commit_all(ws, "builder: extend the affirmative set")
    spec = {"kind": "code", "files_allowed": [SRC], "acceptance_tests": list(acceptance), "test_first": pinned}
    report = qa.evaluate_candidate(settings, ws, spec, ws_mod.changed_files(ws))
    return report, {c.name: c for c in report.checks}, ws


def test_a_real_test_first_is_red_on_base_and_green_on_head(code_settings):
    report, checks, ws = _qa(code_settings, FAILS_ON_BASE)
    assert checks["test_first_red_on_base"].passed and checks["test_first_green_on_head"].passed, report.failures
    assert report.passed, report.failures
    assert not (ws.path.parent / f"{ws.path.name}.test-first-base").exists(), "the base worktree is removed"


def test_a_trivial_test_first_cannot_judge_the_change(code_settings):
    report, checks, _ = _qa(code_settings, TRIVIAL)
    assert not checks["test_first_red_on_base"].passed and "pass on the base" in checks["test_first_red_on_base"].detail
    assert not report.passed and any(f.startswith("test_first_red_on_base") for f in report.failures)


def test_a_test_first_the_head_does_not_satisfy_fails_qa_even_outside_acceptance_tests(code_settings):
    report, checks, _ = _qa(code_settings, NEVER_GREEN, acceptance=TESTS)
    assert checks["test_first_red_on_base"].passed and not checks["test_first_green_on_head"].passed
    assert not report.passed
