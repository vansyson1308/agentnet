"""Builder workspace isolation + independent QA rules."""

from __future__ import annotations

import os
import pathlib
import subprocess
import uuid

import pytest

from services.registry.app.society.engineering import workspace as ws_mod
from services.registry.app.society.engineering.qa import evaluate_candidate, static_security_scan
from services.registry.app.society.intents import FileEdit

ACCEPT = "tests/society/acceptance/test_candidate_docs.py"
DOC = "docs/society/candidates/demo.md"
GOOD_DOC = "# Demo\n\n## Problem\n\np\n\n## Proposed change\n\nc\n\n## Evidence\n\ne\n\n## Verification\n\nv\n"


def _spec(**kw):
    base = {"description": "d", "files_allowed": [DOC], "acceptance_tests": [ACCEPT], "must_compile": True, "kind": "docs"}
    base.update(kw)
    return base


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True).stdout


def test_workspace_is_a_separate_worktree_on_auto_branch(society_settings, temp_repo):
    cid = uuid.uuid4()
    ws = ws_mod.ensure_workspace(society_settings, cid)
    assert ws.path != pathlib.Path(temp_repo) and ws.path.exists()
    assert ws.branch == f"agentnet-auto/{cid}"
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], temp_repo).strip() == "main"
    ws2 = ws_mod.ensure_workspace(society_settings, cid)  # idempotent
    assert ws2.path == ws.path and ws2.branch == ws.branch
    ws_mod.apply_edits(ws, [FileEdit(path=DOC, content=GOOD_DOC)], allowed=[DOC])
    head = ws_mod.commit_all(ws, "society: demo")
    assert head != ws.base_sha
    assert ws_mod.changed_files(ws) == [DOC]
    assert "1 file changed" in ws_mod.diff_stat(ws)
    # main branch never moved and does not contain the file
    assert _git(["rev-parse", "main"], temp_repo).strip() == ws.base_sha
    assert DOC not in _git(["ls-tree", "-r", "--name-only", "main"], temp_repo)
    ws_mod.remove_workspace(society_settings, ws, delete_branch=True)
    assert not ws.path.exists()
    assert ws.branch not in _git(["branch", "--list", "agentnet-auto/*"], temp_repo)


@pytest.mark.parametrize(
    "bad_path",
    ["/etc/passwd", "../outside.md", "docs/../../x.md", "~/x", "docs\\x.md", ".env", ".env.production", "deploy/tls/server.key", "config/secrets/x.json", "id_rsa", "certs/client.pem"],
)
def test_path_containment_and_protected_patterns(society_settings, temp_repo, bad_path):
    ws = ws_mod.ensure_workspace(society_settings, uuid.uuid4())
    with pytest.raises((ws_mod.WorkspaceError, ValueError)):
        edit = FileEdit(path=bad_path, content="x")  # may already be rejected by the schema
        ws_mod.apply_edits(ws, [edit], allowed=[bad_path])
    # nothing was written anywhere
    assert not (ws.path / "outside.md").exists() and not (ws.path.parent / "outside.md").exists()


def test_edit_outside_allow_list_is_rejected_and_nothing_partial_is_written(society_settings, temp_repo):
    ws = ws_mod.ensure_workspace(society_settings, uuid.uuid4())
    edits = [FileEdit(path=DOC, content=GOOD_DOC), FileEdit(path="docs/other.md", content="x")]
    with pytest.raises(ws_mod.WorkspaceError):
        ws_mod.apply_edits(ws, edits, allowed=[DOC])
    assert not (ws.path / DOC).exists(), "first edit must not be written when a later one is rejected"


def test_symlink_escape_is_rejected(society_settings, temp_repo, tmp_path):
    ws = ws_mod.ensure_workspace(society_settings, uuid.uuid4())
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws.path / "docs" / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ws_mod.WorkspaceError):
        ws_mod.apply_edits(ws, [FileEdit(path="docs/link/escape.md", content="x")], allowed=["docs/link/escape.md"])
    assert not (outside / "escape.md").exists()


def test_git_is_never_given_a_shell(monkeypatch, society_settings, temp_repo):
    calls = []
    real_run = subprocess.run

    def spy(argv, **kw):
        calls.append((argv, kw.get("shell", False)))
        return real_run(argv, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    ws = ws_mod.ensure_workspace(society_settings, uuid.uuid4())
    ws_mod.apply_edits(ws, [FileEdit(path=DOC, content=GOOD_DOC)], allowed=[DOC])
    ws_mod.commit_all(ws, "msg; rm -rf / $(evil)")
    assert calls and all(isinstance(a, list) and a[0] == "git" and not sh for a, sh in calls)


def _built(society_settings, content=GOOD_DOC, path=DOC, allowed=None):
    ws = ws_mod.ensure_workspace(society_settings, uuid.uuid4())
    ws_mod.apply_edits(ws, [FileEdit(path=path, content=content)], allowed=allowed or [path])
    ws_mod.commit_all(ws, "c")
    return ws, ws_mod.changed_files(ws)


def test_qa_happy_path_passes(society_settings, temp_repo):
    ws, changed = _built(society_settings)
    report = evaluate_candidate(society_settings, ws, _spec(), changed)
    assert report.passed, report.failures
    assert {c.name for c in report.checks} >= {"acceptance_criteria_present", "allow_list", "protected_paths", "no_self_judging", "acceptance_tests_exist", "compile", "no_secrets_added", "acceptance_tests"}


def test_qa_fails_on_malformed_doc(society_settings, temp_repo):
    ws, changed = _built(society_settings, content="# Demo\n\nno sections\n")
    report = evaluate_candidate(society_settings, ws, _spec(), changed)
    assert not report.passed and any("acceptance_tests" in f for f in report.failures)
    assert "missing section" in report.test_output_tail


def test_qa_never_fabricates_criteria(society_settings, temp_repo):
    ws, changed = _built(society_settings)
    report = evaluate_candidate(society_settings, ws, _spec(acceptance_tests=[]), changed)
    assert not report.passed and any("never fabricates" in f for f in report.failures)


def test_qa_rejects_builder_modifying_its_own_acceptance_test(society_settings, temp_repo):
    ws = ws_mod.ensure_workspace(society_settings, uuid.uuid4())
    # the workspace deny-list already blocks tests/society/*; simulate a spec that used another test path
    other_test = "tests/test_free.py"
    ws_mod.apply_edits(ws, [FileEdit(path=other_test, content="def test_ok():\n    assert True\n")], allowed=[other_test])
    ws_mod.commit_all(ws, "c")
    changed = ws_mod.changed_files(ws)
    report = evaluate_candidate(society_settings, ws, _spec(files_allowed=[other_test], acceptance_tests=[other_test]), changed)
    assert not report.passed and any("no_self_judging" in f for f in report.failures)


def test_qa_rejects_off_list_and_protected_changes(society_settings, temp_repo):
    ws, changed = _built(society_settings, path="docs/other.md", allowed=["docs/other.md"])
    report = evaluate_candidate(society_settings, ws, _spec(), changed)  # spec allows only DOC
    assert not report.passed and any("allow_list" in f for f in report.failures)
    # acceptance tests must not run when gates fail
    assert any("skipped: pre-flight" in c.detail for c in report.checks if c.name == "acceptance_tests")


def test_qa_compile_check_and_secret_scan(society_settings, temp_repo):
    py = "examples/society_demo_module.py"
    ws, changed = _built(society_settings, content="def broken(:\n", path=py)
    report = evaluate_candidate(society_settings, ws, _spec(files_allowed=[py]), changed)
    assert not report.passed and any("compile" in f for f in report.failures)
    ws2, changed2 = _built(society_settings, content="API_KEY = 'sk-" + "a" * 30 + "'\n", path=py)
    report2 = evaluate_candidate(society_settings, ws2, _spec(files_allowed=[py]), changed2)
    assert not report2.passed and any("no_secrets_added" in f for f in report2.failures)
    findings = static_security_scan(ws2, changed2)
    assert any("secret" in f for f in findings)


def test_static_scan_flags_shell_primitives_and_risky_paths(society_settings, temp_repo):
    py = "examples/net.py"
    ws, changed = _built(society_settings, content="import subprocess\nsubprocess.run(['ls'], shell=True)\n", path=py)
    findings = static_security_scan(ws, changed)
    assert any("risky code primitive" in f for f in findings)
    assert os.path.basename(py) == "net.py"


# ── exact-text replacements (graduation: real edits to large files) ─────────

SRC = "services/demo/app.py"
SRC_TEXT = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"


def _ws_with(society_settings, files):
    ws = ws_mod.ensure_workspace(society_settings, uuid.uuid4())
    for rel, text in files.items():
        p = ws.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return ws


def test_replacements_edit_only_the_changed_text(society_settings):
    from services.registry.app.society.intents import TextReplacement

    ws = _ws_with(society_settings, {SRC: SRC_TEXT})
    ws_mod.apply_edits(ws, [FileEdit(path=SRC, replacements=[TextReplacement(old="return 1", new="return 10"), TextReplacement(old="def b():\n    return 2\n", new="def b():\n    return 20\n\n\ndef c():\n    return 3\n")])], allowed=[SRC])
    assert (ws.path / SRC).read_text() == "def a():\n    return 10\n\n\ndef b():\n    return 20\n\n\ndef c():\n    return 3\n"


def test_replacements_see_earlier_edits_to_the_same_file(society_settings):
    from services.registry.app.society.intents import TextReplacement

    ws = _ws_with(society_settings, {SRC: SRC_TEXT})
    ws_mod.apply_edits(ws, [
        FileEdit(path=SRC, replacements=[TextReplacement(old="return 1", new="return 7")]),
        FileEdit(path=SRC, replacements=[TextReplacement(old="return 7", new="return 8")]),
    ], allowed=[SRC])
    assert "return 8" in (ws.path / SRC).read_text()


@pytest.mark.parametrize("old,why", [("return 99", "does not occur"), ("return", "occurs 2 times")])
def test_a_stale_or_ambiguous_replacement_aborts_everything(society_settings, old, why):
    from services.registry.app.society.intents import TextReplacement

    other = "services/demo/other.py"
    ws = _ws_with(society_settings, {SRC: SRC_TEXT, other: "x = 1\n"})
    with pytest.raises(ws_mod.WorkspaceError, match=why):
        ws_mod.apply_edits(ws, [
            FileEdit(path=other, content="x = 2\n"),  # valid, but must not be written
            FileEdit(path=SRC, replacements=[TextReplacement(old=old, new="z")]),
        ], allowed=[SRC, other])
    assert (ws.path / other).read_text() == "x = 1\n" and (ws.path / SRC).read_text() == SRC_TEXT


def test_replacements_cannot_create_a_file_or_bypass_the_allow_list(society_settings):
    from services.registry.app.society.intents import TextReplacement

    ws = _ws_with(society_settings, {SRC: SRC_TEXT})
    with pytest.raises(ws_mod.WorkspaceError, match="existing file"):
        ws_mod.apply_edits(ws, [FileEdit(path="services/demo/new.py", replacements=[TextReplacement(old="a", new="b")])], allowed=["services/demo/new.py"])
    with pytest.raises(ws_mod.WorkspaceError, match="files_allowed"):
        ws_mod.apply_edits(ws, [FileEdit(path=SRC, replacements=[TextReplacement(old="return 1", new="x")])], allowed=["services/demo/other.py"])
    with pytest.raises(ws_mod.WorkspaceError, match="protected"):
        ws_mod.apply_edits(ws, [FileEdit(path=".env.local", replacements=[TextReplacement(old="a", new="b")])], allowed=[".env.local"])


def test_a_file_edit_is_exactly_one_mode():
    from pydantic import ValidationError

    from services.registry.app.society.intents import TextReplacement

    with pytest.raises(ValidationError, match="exactly one"):
        FileEdit(path=SRC)
    with pytest.raises(ValidationError, match="exactly one"):
        FileEdit(path=SRC, content="x", replacements=[TextReplacement(old="a", new="b")])
    with pytest.raises(ValidationError):
        FileEdit(path=SRC, replacements=[])
    with pytest.raises(ValidationError):
        FileEdit(path=SRC, replacements=[TextReplacement(old="", new="b")])
