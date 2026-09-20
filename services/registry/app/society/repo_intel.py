"""Repository intelligence: bounded, typed, READ-ONLY operations for agents.

A Builder or Architect that cannot look at source code is not an engineer;
one that can run a shell is not safe. This module is the only bridge: a
handful of deterministic read operations over either the TRUSTED BASE
checkout (``settings.repo_root``) or a candidate's isolated worktree.

Safety properties (tests/society/test_repo_intel.py):

* paths are normalised; absolute paths, ``..``, ``~``, backslashes and NUL
  are rejected before touching the filesystem;
* the resolved path must stay inside the root after symlink resolution
  (symlinks that point outside are refused, as is reading through one);
* ``.git``, ``.env*``, keys, secret-bearing paths and generated credential
  files are refused (``risk.NEVER_READ_PATTERNS``);
* binary content is refused (NUL byte probe); every result is bounded in
  bytes, lines and entries by settings, never by the caller;
* search is a deterministic, literal or bounded-regex text scan — no shell,
  no external tool;
* results are returned as ``untrusted`` DATA (context.untrusted) so a prompt
  template labels repository contents as data, never instructions.

Persistence and loop protection live in the executor/policy layer: every
read is an ``agent_intents`` row with its result, bounded per run, per
candidate and per correlation, and identical searches are deduplicated.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .risk import is_never_readable

MAX_PATTERN_LEN = 200
MAX_LINE_CHARS = 400
DEFAULT_MAX_BYTES = 32_000
DEFAULT_MAX_LINES = 400
DEFAULT_MAX_ENTRIES = 200
DEFAULT_MAX_RESULTS = 40
DEFAULT_MAX_DEPTH = 4
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".service-envs", ".sdk-envs", ".pytest_cache", ".mypy_cache"}
_TEXT_SUFFIXES = {".py", ".md", ".txt", ".yml", ".yaml", ".json", ".toml", ".ini", ".cfg", ".sql", ".sh", ".html", ".css", ".js", ".ts", ".rst", ".env", ".example", ".jinja", ".j2", ""}


class RepoReadError(Exception):
    """Refused read (path, bounds, binary, secret). Never retried."""


@dataclass
class ReadResult:
    op: str
    path: str = ""
    data: Dict = field(default_factory=dict)
    truncated: bool = False
    bytes_returned: int = 0

    def to_dict(self) -> Dict:
        return {
            "_untrusted": True,
            "source": f"repo:{self.op}",
            "op": self.op,
            "path": self.path,
            "truncated": self.truncated,
            "bytes": self.bytes_returned,
            "data": self.data,
        }


def normalize_rel_path(rel: str) -> str:
    if rel is None:
        raise RepoReadError("path required")
    rel = str(rel)
    if len(rel) > 255 or not rel.strip():
        raise RepoReadError("path too long or empty")
    if rel.startswith(("/", "~")) or "\\" in rel or "\0" in rel:
        raise RepoReadError(f"path not allowed: {rel!r}")
    parts = pathlib.PurePosixPath(rel).parts
    if any(p in ("..", ".") for p in parts):
        raise RepoReadError(f"path traversal rejected: {rel!r}")
    norm = "/".join(parts)
    if norm == ".git" or norm.startswith(".git/") or "/.git/" in norm:
        raise RepoReadError("git internals are not readable")
    if is_never_readable(norm):
        raise RepoReadError(f"path is on the read deny-list: {norm!r}")
    return norm


def contained_read_path(root: pathlib.Path, rel: str) -> pathlib.Path:
    """Resolve ``rel`` inside ``root`` (both symlink-resolved) or refuse."""
    norm = normalize_rel_path(rel)
    root_r = root.resolve()
    target = root_r / norm
    try:
        resolved = target.resolve(strict=True)
    except FileNotFoundError:
        raise RepoReadError(f"not found: {norm}") from None
    except (OSError, RuntimeError) as exc:
        raise RepoReadError(f"unreadable: {norm}: {type(exc).__name__}") from None
    if resolved != root_r and root_r not in resolved.parents:
        raise RepoReadError(f"path escapes repository: {norm!r}")
    # every component must be a real directory/file, not a symlink out of tree
    probe = target
    while probe != root_r:
        if probe.is_symlink():
            raise RepoReadError(f"refusing to read through symlink: {norm!r}")
        probe = probe.parent
    return resolved


def _is_binary(sample: bytes) -> bool:
    return b"\0" in sample


def _read_text_bounded(path: pathlib.Path, max_bytes: int) -> tuple[str, bool]:
    with open(path, "rb") as fh:
        raw = fh.read(max_bytes + 1)
    if _is_binary(raw[:8192]):
        raise RepoReadError("binary file refused")
    truncated = len(raw) > max_bytes
    text = raw[:max_bytes].decode("utf-8", errors="replace")
    return text, truncated


def list_tree(root: pathlib.Path, rel: str = "", *, depth: int = 2, max_entries: int = DEFAULT_MAX_ENTRIES) -> ReadResult:
    depth = max(1, min(int(depth or 1), DEFAULT_MAX_DEPTH))
    max_entries = max(1, min(int(max_entries or DEFAULT_MAX_ENTRIES), DEFAULT_MAX_ENTRIES))
    base = contained_read_path(root, rel) if rel else root.resolve()
    if not base.is_dir():
        raise RepoReadError(f"not a directory: {rel or '.'}")
    root_r = root.resolve()
    entries: List[Dict] = []
    truncated = False

    def walk(d: pathlib.Path, level: int) -> None:
        nonlocal truncated
        try:
            children = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        except OSError:
            return
        for child in children:
            if len(entries) >= max_entries:
                truncated = True
                return
            if child.name in _SKIP_DIRS or child.is_symlink():
                continue
            relp = child.relative_to(root_r).as_posix()
            if is_never_readable(relp):
                continue
            if child.is_dir():
                entries.append({"path": relp, "type": "dir"})
                if level < depth:
                    walk(child, level + 1)
            else:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = None
                entries.append({"path": relp, "type": "file", "size": size})

    walk(base, 1)
    return ReadResult(op="list_tree", path=rel or ".", data={"entries": entries, "count": len(entries)}, truncated=truncated, bytes_returned=sum(len(e["path"]) for e in entries))


def _iter_files(root: pathlib.Path, glob: Optional[str]) -> List[pathlib.Path]:
    root_r = root.resolve()
    out: List[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(root_r):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not (pathlib.Path(dirpath) / d).is_symlink())
        for name in sorted(filenames):
            p = pathlib.Path(dirpath) / name
            if p.is_symlink():
                continue
            relp = p.relative_to(root_r).as_posix()
            if is_never_readable(relp):
                continue
            if glob and not (fnmatch.fnmatchcase(relp, glob) or fnmatch.fnmatchcase(name, glob)):
                continue
            if p.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            out.append(p)
    return out


def search(root: pathlib.Path, pattern: str, *, glob: Optional[str] = None, regex: bool = False, max_results: int = DEFAULT_MAX_RESULTS, max_files: int = 4000) -> ReadResult:
    if not pattern or len(pattern) > MAX_PATTERN_LEN:
        raise RepoReadError("pattern must be 1..200 chars")
    if glob and (len(glob) > 120 or "\0" in glob or glob.startswith(("/", "~"))):
        raise RepoReadError("bad glob")
    max_results = max(1, min(int(max_results or DEFAULT_MAX_RESULTS), DEFAULT_MAX_RESULTS))
    if regex:
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            raise RepoReadError(f"bad regex: {exc}") from None
        matcher = lambda line: rx.search(line) is not None  # noqa: E731
    else:
        needle = pattern.lower()
        matcher = lambda line: needle in line.lower()  # noqa: E731
    root_r = root.resolve()
    hits: List[Dict] = []
    truncated = False
    scanned = 0
    for p in _iter_files(root, glob):
        scanned += 1
        if scanned > max_files:
            truncated = True
            break
        try:
            with open(p, "rb") as fh:
                raw = fh.read(DEFAULT_MAX_BYTES * 8)
        except OSError:
            continue
        if _is_binary(raw[:4096]):
            continue
        text = raw.decode("utf-8", errors="replace")
        relp = p.relative_to(root_r).as_posix()
        for n, line in enumerate(text.splitlines(), 1):
            if matcher(line):
                hits.append({"path": relp, "line": n, "text": line.strip()[:MAX_LINE_CHARS]})
                if len(hits) >= max_results:
                    truncated = True
                    break
        if truncated:
            break
    return ReadResult(op="search", path=glob or "", data={"pattern": pattern[:MAX_PATTERN_LEN], "hits": hits, "count": len(hits), "files_scanned": scanned}, truncated=truncated, bytes_returned=sum(len(h["text"]) for h in hits))


def read_file(root: pathlib.Path, rel: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> ReadResult:
    max_bytes = max(256, min(int(max_bytes or DEFAULT_MAX_BYTES), DEFAULT_MAX_BYTES))
    p = contained_read_path(root, rel)
    if not p.is_file():
        raise RepoReadError(f"not a file: {rel}")
    text, truncated = _read_text_bounded(p, max_bytes)
    norm = normalize_rel_path(rel)
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    data = {"content": text, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "lines": lines}
    if truncated:
        # A truncated read has to say how to CONTINUE it. The cut is by bytes and
        # can land mid-line, so the last returned line may be partial: re-reading
        # it is correct, skipping it silently loses content. A live Builder with
        # only a byte count to go on continued a 12000-byte preview at "line"
        # 12000 of a 92-line file and stranded its candidate.
        whole, _ = _read_text_bounded(p, DEFAULT_MAX_BYTES * 8)
        data["total_lines"] = whole.count("\n") + (1 if whole and not whole.endswith("\n") else 0)
        data["next_line"] = max(1, lines)
    return ReadResult(op="read_file", path=norm, data=data, truncated=truncated, bytes_returned=len(text))


def read_range(root: pathlib.Path, rel: str, start: int, end: int, *, max_lines: int = DEFAULT_MAX_LINES) -> ReadResult:
    start = max(1, int(start or 1))
    end = max(start, int(end or start))
    max_lines = max(1, min(int(max_lines or DEFAULT_MAX_LINES), DEFAULT_MAX_LINES))
    if end - start + 1 > max_lines:
        end = start + max_lines - 1
    p = contained_read_path(root, rel)
    if not p.is_file():
        raise RepoReadError(f"not a file: {rel}")
    text, _ = _read_text_bounded(p, DEFAULT_MAX_BYTES * 8)
    lines = text.splitlines()
    chunk = lines[start - 1 : end]
    body = "\n".join(ln[:MAX_LINE_CHARS] for ln in chunk)
    norm = normalize_rel_path(rel)
    return ReadResult(op="read_range", path=norm, data={"start": start, "end": min(end, len(lines)), "total_lines": len(lines), "content": body}, truncated=end < len(lines) or start > len(lines), bytes_returned=len(body))


def diff_result(diff_text: str, changed_files: Sequence[str], *, max_bytes: int = DEFAULT_MAX_BYTES) -> ReadResult:
    body = (diff_text or "")[:max_bytes]
    return ReadResult(op="read_diff", data={"changed_files": list(changed_files)[:50], "diff": body}, truncated=len(diff_text or "") > max_bytes, bytes_returned=len(body))


def search_key(pattern: str, glob: Optional[str], regex: bool, scope: str) -> str:
    """Stable identity of a search for duplicate suppression."""
    return hashlib.sha256(f"{scope}|{regex}|{glob or ''}|{pattern.strip().lower()}".encode("utf-8")).hexdigest()[:32]


__all__ = [
    "RepoReadError",
    "ReadResult",
    "normalize_rel_path",
    "contained_read_path",
    "list_tree",
    "search",
    "read_file",
    "read_range",
    "diff_result",
    "search_key",
]
