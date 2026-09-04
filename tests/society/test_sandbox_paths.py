"""Builder/QA sandbox: path-escape attempts beyond the basic containment tests.

The Builder may only write files that are (a) inside its own worktree after
symlink resolution, (b) on the candidate's allow-list and (c) not protected.
Every attempt below must be refused before anything is written."""

from __future__ import annotations

import os
import pathlib

import pytest

from services.registry.app.society.engineering import workspace as ws_mod
from services.registry.app.society.engineering.workspace import WorkspaceError, contained_path, is_protected

ESCAPES = [
    "docs/back\\slash.md",  # backslashes are refused outright (Windows separators, escape tricks)
    "/etc/passwd",
    "//etc/passwd",
    "../outside.md",
    "docs/../../outside.md",
    "docs/./../../outside.md",
    "docs/..",
    ".",
    "",
    "docs/\x00hidden.md",
]
PROTECTED = [
    ".env",
    ".env.local",
    ".ENV.example",  # patterns are matched case-insensitively for dotfiles
    ".git/config",
    ".git/hooks/pre-commit",
    ".github/workflows/ci.yml",
    "services/registry/app/config.py",
    "services/registry/app/auth.py",
    "services/registry/app/society/policy.py",
    "services/registry/init-db/99-evil.sql",
    "services/registry/migrations/versions/0099_evil.py",
    "docker-compose.staging.yml",
    "deploy/legacy-vps/runbook-prod.sh",
    "sdk/python/agentnet/client.py",
    "tests/society/conftest.py",
    "docs/secrets/notes.md",
    "keys/server.pem",
    "id_rsa.key",
    "requirements.txt",
    "services/worker/requirements-dev.txt",
]


@pytest.mark.parametrize("rel", ESCAPES)
def test_paths_that_leave_the_worktree_are_refused(tmp_path, rel):
    root = tmp_path / "ws"
    root.mkdir()
    with pytest.raises((WorkspaceError, ValueError)):
        contained_path(root, rel)


@pytest.mark.parametrize("rel", PROTECTED)
def test_protected_paths_are_recognised(rel):
    assert is_protected(rel), rel


def test_unicode_and_odd_but_contained_names_stay_inside(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    for rel in ("docs/ünïcode-note.md", "docs/space name.md", "docs/․․/x.md"):
        target = contained_path(root, rel)
        assert root.resolve() in target.parents


def test_nested_symlink_chain_cannot_escape(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "docs").mkdir()
    os.symlink(outside, root / "docs" / "link1")
    os.symlink(root / "docs" / "link1", root / "docs" / "link2")
    with pytest.raises(WorkspaceError):
        contained_path(root, "docs/link2/escaped.md")
    # a symlinked FILE inside the tree is refused too (write-through)
    real = outside / "real.md"
    real.write_text("x")
    os.symlink(real, root / "docs" / "filelink.md")
    with pytest.raises(WorkspaceError):
        contained_path(root, "docs/filelink.md")


def test_apply_edits_refuses_protected_and_off_list_before_writing(society_settings, temp_repo):
    import uuid

    from services.registry.app.society.intents import FileEdit

    ws = ws_mod.ensure_workspace(society_settings, uuid.uuid4())
    try:
        before = sorted(p.relative_to(ws.path).as_posix() for p in ws.path.rglob("*") if p.is_file() and ".git" not in p.parts)
        # FileEdit validates paths itself; model_construct bypasses that layer on
        # purpose so the workspace guard is proven to hold on its own.
        for bad in (".env", "../escape.md", "docs/../../escape.md", "services/registry/app/auth.py"):
            with pytest.raises(WorkspaceError):
                ws_mod.apply_edits(ws, [FileEdit.model_construct(path=bad, content="x")], allowed=[bad, "docs/ok.md"])
        # allow-list is exact: a legitimate file plus one escape means nothing is written
        with pytest.raises(WorkspaceError):
            ws_mod.apply_edits(ws, [FileEdit(path="docs/ok.md", content="fine"), FileEdit.model_construct(path="../escape.md", content="x")], allowed=["docs/ok.md", "../escape.md"])
        after = sorted(p.relative_to(ws.path).as_posix() for p in ws.path.rglob("*") if p.is_file() and ".git" not in p.parts)
        assert before == after, "a refused batch must not leave partial writes"
    finally:
        ws_mod.remove_workspace(society_settings, ws, delete_branch=True)


def test_commit_message_and_branch_names_are_never_shell_or_option_injected(society_settings, temp_repo):
    import uuid

    from services.registry.app.society.intents import FileEdit

    cid = uuid.uuid4()
    assert ws_mod.branch_name(society_settings, cid).startswith("agentnet-auto/") and " " not in ws_mod.branch_name(society_settings, cid)
    ws = ws_mod.ensure_workspace(society_settings, cid)
    try:
        ws_mod.apply_edits(ws, [FileEdit(path="docs/ok.md", content="fine\n")], allowed=["docs/ok.md"])
        sha = ws_mod.commit_all(ws, "--amend; rm -rf / $(touch /tmp/pwned) `id`")
        assert len(sha) >= 7
        log = ws_mod._git(["log", "-1", "--format=%s"], cwd=ws.path)
        assert log.startswith("--amend")  # stored as text, not interpreted
    finally:
        ws_mod.remove_workspace(society_settings, ws, delete_branch=True)
