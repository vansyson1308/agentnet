"""Typed, atomic patch sets for the repair Builder (ADR-0010 D8).

A model never sends a shell patch or a unified diff to apply. It sends a
``PatchSet``: per file, a list of exact-text operations:

* ``replace_exact``  -- ``old`` must occur EXACTLY once; replaced by ``new``;
* ``insert_after``   -- ``anchor`` must occur exactly once; ``text`` follows it;
* ``insert_before``  -- ``anchor`` must occur exactly once; ``text`` precedes it;
* ``create``         -- the file must not exist; ``text`` is its content;
* ``delete``         -- restricted: only a file this repair attempt created
                        (never a file present at ``base_sha``).

Application is all-or-nothing: every operation of every file is matched and
validated against the in-memory text first; if anything is ambiguous, stale,
outside the plan revision's ``files_allowed``, protected, escaping the
workspace or oversized, NOTHING is written. A ``base_sha`` that is not the
workspace's base fails as stale (the reconciler rebuilds on the current base).

Small edits to large files are the point: altering 12 lines of a 12 KB file is
two short operations, not a 12 KB answer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import List, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..society.engineering import workspace as ws_mod

MAX_OPS_PER_FILE = 24
MAX_FILES = 12
MAX_OP_TEXT = 40_000
MAX_FILE_BYTES = 400_000


class PatchOp(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    op: Literal["replace_exact", "insert_after", "insert_before", "create", "delete"]
    old: Optional[str] = Field(default=None, max_length=MAX_OP_TEXT)
    new: Optional[str] = Field(default=None, max_length=MAX_OP_TEXT)
    anchor: Optional[str] = Field(default=None, max_length=MAX_OP_TEXT)
    text: Optional[str] = Field(default=None, max_length=MAX_OP_TEXT)

    def required(self) -> None:
        need = {
            "replace_exact": ("old", "new"),
            "insert_after": ("anchor", "text"),
            "insert_before": ("anchor", "text"),
            "create": ("text",),
            "delete": (),
        }[self.op]
        for name in need:
            if getattr(self, name) is None:
                raise PatchError(f"{self.op} needs '{name}'")
        if self.op == "replace_exact" and not self.old:
            raise PatchError("replace_exact needs a non-empty 'old'")
        if self.op in ("insert_after", "insert_before") and not self.anchor:
            raise PatchError(f"{self.op} needs a non-empty 'anchor'")


class FilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=300)
    operations: List[PatchOp] = Field(min_length=1, max_length=MAX_OPS_PER_FILE)

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        if v.startswith(("/", "~")) or "\\" in v or "\0" in v or ".." in v.split("/"):
            raise ValueError("path must be repository-relative")
        return v


class PatchSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_sha: str = Field(min_length=7, max_length=64)
    files: List[FilePatch] = Field(min_length=1, max_length=MAX_FILES)

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode("utf-8")).hexdigest()


class PatchError(Exception):
    """A patch set was rejected; nothing was written."""

    def __init__(self, message: str, *, code: str = "invalid_patch"):
        super().__init__(message)
        self.code = code


@dataclass
class AppliedPatch:
    written: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    digest: str = ""


def _count(text: str, needle: str) -> int:
    return text.count(needle)


def _once(text: str, needle: str, rel: str, what: str, i: int) -> int:
    n = _count(text, needle)
    if n == 0:
        raise PatchError(f"{rel}: operation {i}: the {what} text does not occur in the current file (stale or wrong)", code="no_match")
    if n > 1:
        raise PatchError(f"{rel}: operation {i}: the {what} text occurs {n} times; include more surrounding lines so it is unique", code="ambiguous")
    return text.index(needle)


def _exists_at_base(ws: ws_mod.Workspace, rel: str) -> bool:
    try:
        ws_mod._git(["cat-file", "-e", f"{ws.base_sha}:{rel}"], cwd=ws.path)
        return True
    except ws_mod.WorkspaceError:
        return False


def apply_patchset(ws: ws_mod.Workspace, patch: PatchSet, *, files_allowed: Sequence[str], protected: Sequence[str] = ()) -> AppliedPatch:
    """Validate everything, then write. Raises :class:`PatchError` (nothing
    written) on any problem."""
    if not ws.base_sha.startswith(patch.base_sha):
        raise PatchError(f"stale base: patch is for {patch.base_sha[:10]}, workspace is on {ws.base_sha[:10]}", code="stale_base")
    allowed = {a.replace("\\", "/") for a in files_allowed}
    seen_paths = set()
    plan: List[tuple] = []
    for fp in patch.files:
        rel = fp.path
        if rel in seen_paths:
            raise PatchError(f"{rel}: listed twice; put every operation for a file in one entry", code="duplicate_file")
        seen_paths.add(rel)
        if rel not in allowed:
            raise PatchError(f"{rel} is outside the plan revision's files_allowed; return NEEDS_RESCOPE instead", code="out_of_scope")
        if ws_mod.is_protected(rel) or any(rel == p or rel.startswith(p.rstrip("*").rstrip("/") + "/") for p in protected):
            raise PatchError(f"{rel} is a protected path", code="protected")
        try:
            target = ws_mod.contained_path(ws.path, rel)
        except ws_mod.WorkspaceError as exc:
            raise PatchError(str(exc), code="path") from exc
        exists = target.is_file()
        text: Optional[str] = target.read_text(encoding="utf-8") if exists else None
        delete = False
        for i, op in enumerate(fp.operations, 1):
            op.required()
            if op.op == "create":
                if text is not None:
                    raise PatchError(f"{rel}: create on an existing file; use replace_exact/insert_*", code="exists")
                text = op.text or ""
            elif op.op == "delete":
                if len(fp.operations) != 1:
                    raise PatchError(f"{rel}: delete must be the file's only operation", code="invalid_patch")
                if not exists:
                    raise PatchError(f"{rel}: delete of a missing file", code="no_match")
                if _exists_at_base(ws, rel):
                    raise PatchError(f"{rel}: deleting a file that exists on the base revision is not allowed", code="protected")
                delete = True
            else:
                if text is None:
                    raise PatchError(f"{rel}: {op.op} on a missing file; use create", code="no_match")
                if op.op == "replace_exact":
                    at = _once(text, op.old or "", rel, "'old'", i)
                    text = text[:at] + (op.new or "") + text[at + len(op.old or ""):]
                elif op.op == "insert_after":
                    at = _once(text, op.anchor or "", rel, "'anchor'", i) + len(op.anchor or "")
                    text = text[:at] + (op.text or "") + text[at:]
                else:
                    at = _once(text, op.anchor or "", rel, "'anchor'", i)
                    text = text[:at] + (op.text or "") + text[at:]
            if text is not None and len(text.encode("utf-8")) > MAX_FILE_BYTES:
                raise PatchError(f"{rel}: result exceeds {MAX_FILE_BYTES} bytes", code="too_large")
        plan.append((target, rel, None if delete else text))
    out = AppliedPatch(digest=patch.digest())
    for target, rel, text in plan:
        if text is None:
            target.unlink()
            out.deleted.append(rel)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            out.written.append(rel)
    return out


def reset_to_base(ws: ws_mod.Workspace) -> None:
    """Discard every uncommitted and committed change of this attempt: the
    workspace is back on ``base_sha`` (used when an attempt restarts after a
    crash, so a replayed patch can never double-apply)."""
    ws_mod._git(["reset", "-q", "--hard", ws.base_sha], cwd=ws.path)
    ws_mod._git(["clean", "-q", "-fd"], cwd=ws.path)


def workspace_file_exists(ws: ws_mod.Workspace, rel: str) -> bool:
    try:
        return ws_mod.contained_path(ws.path, rel).is_file()
    except ws_mod.WorkspaceError:
        return False


__all__ = ["PatchOp", "FilePatch", "PatchSet", "PatchError", "AppliedPatch", "apply_patchset", "reset_to_base", "workspace_file_exists"]
