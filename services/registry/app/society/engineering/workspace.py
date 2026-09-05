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

import fnmatch
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

logger = logging.getLogger(__name__)

GIT_AUTHOR = ("AgentNet Society Builder", "society-builder@agentnet.local")

# Paths the autonomous Builder may NEVER write, whatever the spec says.
# Deny-list is evaluated with fnmatch on the POSIX relative path.
PROTECTED_PATTERNS: Sequence[str] = (
    ".env*",
    ".git/*",
    ".github/*",
    ".gitignore",
    "*.pem",
    "*.key",
    "*secret*",
    "*/secrets/*",
    "docker-compose*.yml",
    "deploy/*",
    "*/Dockerfile",
    "Dockerfile",
    "*requirements*.txt",
    "services/registry/init-db/*",
    "services/registry/migrations/*",
    "services/*/app/config.py",
    "services/*/app/auth.py",
    "services/*/app/security.py",
    "services/registry/app/task_service.py",
    "services/registry/app/task_contract.py",
    "services/payment/*",
    "services/registry/app/society/*",
    "tests/society/*",
    "sdk/*",
)


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
    """Case-insensitive on purpose: the worktree may live on a
    case-insensitive filesystem (macOS/Windows) where ``.ENV`` opens
    ``.env``. ``fnmatch.fnmatch`` is case-sensitive on POSIX, so we fold
    both sides ourselves."""
    p = rel_path.replace(os.sep, "/").lower()
    for raw in PROTECTED_PATTERNS:
        pat = raw.lower()
        if fnmatch.fnmatchcase(p, pat) or fnmatch.fnmatchcase(p, "*/" + pat) or p.startswith(pat.rstrip("*")):
            return True
    return False


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


def apply_edits(ws: Workspace, edits: Iterable[FileEdit], allowed: Sequence[str]) -> List[str]:
    """Validate every edit first, then write. Returns written relative paths."""
    allowed_set = {a.replace(os.sep, "/") for a in allowed}
    plan: List[tuple[pathlib.Path, str, str]] = []
    for edit in edits:
        rel = edit.path.replace(os.sep, "/")
        if rel not in allowed_set:
            raise WorkspaceError(f"{rel!r} is not on the candidate's files_allowed list")
        if is_protected(rel):
            raise WorkspaceError(f"{rel!r} matches a protected path pattern")
        target = contained_path(ws.path, rel)
        plan.append((target, rel, edit.content))
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
