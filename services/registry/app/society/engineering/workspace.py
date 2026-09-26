"""Isolated Builder workspaces: one git worktree per code candidate.

Safety properties (all tested in tests/society/test_engineering.py):

* The Builder never touches the production checkout. Every candidate gets
  ``git worktree add -B <prefix>/<candidate-id> <workspace_root>/<id> <base>``
  and all edits/commits happen there.
* Path containment: an edit path must (a) be relative, (b) resolve inside
  the worktree after symlink resolution, (c) be on the candidate's
  ``files_allowed`` list exactly, and (d) not match ``PROTECTED_PATTERNS``.
  A violation aborts the whole submission (nothing partial is written).
* Commits are authored by a fixed non-human identity and never pushed.
  There is no code path here that runs ``git push``, ``merge`` or
  ``checkout`` on the main worktree.
* ``git`` is invoked with an explicit argv list, never through a shell, and
  no model-provided string is ever an argument other than file *contents*.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import re
import subprocess
import uuid
from dataclasses import dataclass
from typing import Iterable, List, Sequence

from ..config import SocietySettings
from ..intents import FileEdit
from ..risk import NEVER_WRITE_PATTERNS, is_never_writable

logger = logging.getLogger(__name__)

GIT_AUTHOR = ("AgentNet Society Builder", "society-builder@agentnet.local")

# Paths the autonomous Builder may NEVER write, whatever the spec says:
# secret material and git internals (risk.NEVER_WRITE_PATTERNS). Everything
# else is writable and classified by the TRUSTED risk tier (risk.py): RED
# surfaces (society runtime, auth, payment, migrations, deploy, CI...) may be
# proposed but always need Security + human approval before promotion.
PROTECTED_PATTERNS: Sequence[str] = tuple(NEVER_WRITE_PATTERNS)


class WorkspaceError(Exception):
    pass


@dataclass
class Workspace:
    candidate_id: uuid.UUID
    path: pathlib.Path
    branch: str
    base_sha: str
    repo_root: pathlib.Path


def _git(args: Sequence[str], *, cwd: pathlib.Path, timeout: int = 60) -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "GIT_AUTHOR_NAME": GIT_AUTHOR[0],
        "GIT_AUTHOR_EMAIL": GIT_AUTHOR[1],
        "GIT_COMMITTER_NAME": GIT_AUTHOR[0],
        "GIT_COMMITTER_EMAIL": GIT_AUTHOR[1],
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceError(f"git {' '.join(args[:2])} failed: {exc}") from exc
    if proc.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args[:2])} failed ({proc.returncode}): {proc.stderr.strip()[:500]}")
    return proc.stdout


def branch_name(settings: SocietySettings, candidate_id: uuid.UUID) -> str:
    prefix = re.sub(r"[^A-Za-z0-9._/-]", "-", settings.branch_prefix).strip("/") or "agentnet-auto"
    return f"{prefix}/{candidate_id}"


def is_protected(rel_path: str) -> bool:
    """NEVER-writable path (secret material, git internals). Case-insensitive
    on purpose: the worktree may live on a case-insensitive filesystem where
    ``.ENV`` opens ``.env`` (risk.is_never_writable folds case)."""
    return is_never_writable(rel_path.replace(os.sep, "/"))


def contained_path(ws_root: pathlib.Path, rel_path: str) -> pathlib.Path:
    """Resolve rel_path inside ws_root or raise WorkspaceError."""
    if not rel_path or rel_path.startswith(("/", "~")) or "\\" in rel_path or "\0" in rel_path:
        raise WorkspaceError(f"path not allowed: {rel_path!r}")
    parts = pathlib.PurePosixPath(rel_path).parts
    if not parts or any(p in ("..", ".") for p in parts):
        # "" / "." / "./" collapse to the workspace root itself, which is a
        # directory, never a file a candidate may write.
        raise WorkspaceError(f"path traversal rejected: {rel_path!r}")
    root = ws_root.resolve()
    target = (root / rel_path)
    # Resolve the deepest existing ancestor to defeat symlink escapes.
    probe = target
    while not probe.exists() and probe != root:
        probe = probe.parent
    if root not in probe.resolve().parents and probe.resolve() != root:
        raise WorkspaceError(f"path escapes workspace: {rel_path!r}")
    if target.exists() and target.is_symlink():
        raise WorkspaceError(f"refusing to write through symlink: {rel_path!r}")
    return target


def ensure_workspace(settings: SocietySettings, candidate_id: uuid.UUID, *, base_ref: str = "HEAD") -> Workspace:
    """Create (or reuse) the worktree for a candidate. Idempotent."""
    repo_root = pathlib.Path(settings.repo_root).resolve()
    if not (repo_root / ".git").exists():
        raise WorkspaceError(f"SOCIETY_REPO_ROOT {repo_root} is not a git repository")
    ws_root = pathlib.Path(settings.workspace_root).resolve()
    ws_root.mkdir(parents=True, exist_ok=True)
    path = ws_root / str(candidate_id)
    branch = branch_name(settings, candidate_id)

    if (path / ".git").exists():
        base_sha = _git(["merge-base", branch, base_ref], cwd=repo_root).strip() or _git(["rev-parse", base_ref], cwd=repo_root).strip()
        return Workspace(candidate_id=candidate_id, path=path, branch=branch, base_sha=base_sha, repo_root=repo_root)

    base_sha = _git(["rev-parse", base_ref], cwd=repo_root).strip()
    # Stale registration (e.g. directory deleted): prune before re-adding.
    _git(["worktree", "prune"], cwd=repo_root)
    _git(["worktree", "add", "-B", branch, str(path), base_sha], cwd=repo_root, timeout=120)
    logger.info("society workspace created: %s on %s (base %s)", path, branch, base_sha[:10])
    return Workspace(candidate_id=candidate_id, path=path, branch=branch, base_sha=base_sha, repo_root=repo_root)


def _apply_replacements(target: pathlib.Path, rel: str, replacements, planned: dict) -> str:
    """Resolve exact-text replacements against the file as it currently
    stands (including earlier edits to the same path in this submission).
    Each ``old`` must occur EXACTLY once: zero is a stale edit, more than one
    is ambiguous -- both abort the whole submission."""
    if rel in planned:
        text = planned[rel]
    elif target.is_file():
        text = target.read_text(encoding="utf-8")
    else:
        raise WorkspaceError(f"{rel!r}: replacements need an existing file (send 'content' to create one)")
    for i, r in enumerate(replacements, 1):
        count = text.count(r.old)
        if count == 0:
            raise WorkspaceError(f"{rel!r}: replacement {i}: the 'old' text does not occur in the current file")
        if count > 1:
            raise WorkspaceError(f"{rel!r}: replacement {i}: the 'old' text occurs {count} times; include more surrounding lines so it is unique")
        text = text.replace(r.old, r.new, 1)
    return text


def apply_edits(ws: Workspace, edits: Iterable[FileEdit], allowed: Sequence[str]) -> List[str]:
    """Validate every edit first, then write. Returns written relative paths.
    A whole-file ``content`` replaces the file; ``replacements`` are exact-text
    edits of the existing file. Nothing is written unless every edit is valid."""
    allowed_set = {a.replace(os.sep, "/") for a in allowed}
    plan: List[tuple[pathlib.Path, str, str]] = []
    planned: dict = {}
    for edit in edits:
        rel = edit.path.replace(os.sep, "/")
        if rel not in allowed_set:
            raise WorkspaceError(f"{rel!r} is not on the candidate's files_allowed list")
        if is_protected(rel):
            raise WorkspaceError(f"{rel!r} matches a protected path pattern")
        target = contained_path(ws.path, rel)
        content = edit.content if edit.replacements is None else _apply_replacements(target, rel, edit.replacements, planned)
        planned[rel] = content
        plan.append((target, rel, content))
    written = []
    for target, rel, content in plan:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(rel)
    return written


def commit_all(ws: Workspace, message: str) -> str:
    """Stage every change in the worktree and commit. Returns head sha.
    A no-op (nothing changed) returns the current head."""
    _git(["add", "-A"], cwd=ws.path)
    status = _git(["status", "--porcelain"], cwd=ws.path)
    if not status.strip():
        return _git(["rev-parse", "HEAD"], cwd=ws.path).strip()
    safe_message = re.sub(r"[^\x20-\x7e\n]", "", message)[:500] or "society: candidate change"
    _git(["commit", "-q", "-m", safe_message], cwd=ws.path)
    return _git(["rev-parse", "HEAD"], cwd=ws.path).strip()


def changed_files(ws: Workspace) -> List[str]:
    out = _git(["diff", "--name-only", f"{ws.base_sha}..HEAD"], cwd=ws.path)
    files = [ln.strip() for ln in out.splitlines() if ln.strip()]
    # include uncommitted changes too (defensive; should be empty after commit)
    out2 = _git(["status", "--porcelain"], cwd=ws.path)
    for ln in out2.splitlines():
        p = ln[3:].strip()
        if p and p not in files:
            files.append(p)
    return files


def diff_stat(ws: Workspace) -> str:
    return _git(["diff", "--stat", f"{ws.base_sha}..HEAD"], cwd=ws.path)[:4000]


def diff_text(ws: Workspace, max_chars: int = 20000) -> str:
    return _git(["diff", f"{ws.base_sha}..HEAD"], cwd=ws.path)[:max_chars]


def diff_identity(ws: Workspace) -> tuple[str, int]:
    """(sha256 of the canonical diff, number of +/- lines). Whitespace-only
    hunks still count as lines; format-only churn is judged by the caller."""
    diff = _git(["diff", f"{ws.base_sha}..HEAD"], cwd=ws.path)
    lines = [ln for ln in diff.splitlines() if (ln.startswith("+") or ln.startswith("-")) and not ln.startswith(("+++", "---"))]
    canon = "\n".join(lines)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest(), len(lines)


def is_format_only(ws: Workspace) -> bool:
    """True when the diff changes nothing but whitespace/blank lines."""
    diff = _git(["diff", "--ignore-all-space", "--ignore-blank-lines", f"{ws.base_sha}..HEAD"], cwd=ws.path)
    return not any(ln.startswith(("+", "-")) and not ln.startswith(("+++", "---")) for ln in diff.splitlines())


def head_sha(ws: Workspace) -> str:
    return _git(["rev-parse", "HEAD"], cwd=ws.path).strip()


def remove_workspace(settings: SocietySettings, ws: Workspace, *, delete_branch: bool = False) -> None:
    repo_root = ws.repo_root
    try:
        _git(["worktree", "remove", "--force", str(ws.path)], cwd=repo_root)
    except WorkspaceError as exc:
        logger.warning("worktree remove failed (%s); pruning", exc)
        _git(["worktree", "prune"], cwd=repo_root)
    if delete_branch:
        try:
            _git(["branch", "-D", ws.branch], cwd=repo_root)
        except WorkspaceError as exc:
            logger.warning("branch delete failed: %s", exc)


def main_branch_head(settings: SocietySettings) -> str:
    return _git(["rev-parse", "HEAD"], cwd=pathlib.Path(settings.repo_root)).strip()
