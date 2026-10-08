"""The repair coding harness: one isolated worktree per RepairAttempt.

Within one bounded attempt the Builder activity iterates

    read -> diagnose -> patch -> targeted test -> inspect failure -> patch -> test

through tools bound to the attempt's worktree. It is a local loop: a failing
unit test is feedback for the next turn, not a new proposal.

Crash safety: an attempt's worktree is derived deterministically from
``(case_id, attempt)``. Every (re)start of an attempt resets it to its base
first, so a replayed attempt can never apply a patch twice. Nothing here
pushes, merges or talks to GitHub.

Verification is deterministic: the existing independent QA evaluator
(``society/engineering/qa.py``: allow-list, protected paths, no self-judging,
compile, secret scan, acceptance tests in a scrubbed environment) plus the
trusted maintenance risk classification. Model reviews are extra scrutiny:
they can fail a patch, never pass one the deterministic gates failed.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..society.config import SocietySettings
from ..society.engineering import qa as qa_mod
from ..society.engineering import workspace as ws_mod
from . import policy as policy_mod
from .config import MaintenanceSettings
from .patchset import PatchError, PatchSet, apply_patchset, reset_to_base
from .repo_tools import RepoToolError, RepoTools, page_text

_NS = uuid.UUID("5f2d3c1e-8a4b-4c7d-9e10-6b7a8c9d0e1f")
_FAILED_RE = re.compile(r"^(FAILED|ERROR) (\S+)(?: - (.*))?$")
_CITE_RE = re.compile(r"([\w./-]+\.\w+):(\d+)(?:-(\d+))?")
CONTEXT_WHOLE_FILE_BYTES = 14_000
CONTEXT_TOTAL_BYTES = 30_000
CONTEXT_WINDOW_LINES = 40


def target_file_context(root, files_allowed: Sequence[str], citations: Sequence[str], *, whole_file_bytes: int = CONTEXT_WHOLE_FILE_BYTES,
                        total_bytes: int = CONTEXT_TOTAL_BYTES) -> List[Dict[str, Any]]:
    """Front-loaded AuthorPatch context: each non-test target file whole when it
    is small, else the line windows the diagnosis/plan cited (``path:line``),
    else its first page. Bounded; files outside the worktree are skipped."""
    import pathlib  # noqa: PLC0415

    root = pathlib.Path(root).resolve()
    cites: Dict[str, List[tuple]] = {}
    for text in citations:
        for m in _CITE_RE.finditer(str(text)):
            a = int(m.group(2))
            cites.setdefault(m.group(1).lstrip("./"), []).append((a, int(m.group(3) or a)))
    out: List[Dict[str, Any]] = []
    used = 0
    for rel in files_allowed:
        target = (root / rel).resolve()
        if "/tests/" in f"/{rel}" or not target.is_file() or root not in target.parents:
            continue
        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if len(text.encode("utf-8")) <= whole_file_bytes:
            entry = {"path": rel, "mode": "whole", "lines": len(lines), "content": text}
        else:
            spans = [(max(1, a - CONTEXT_WINDOW_LINES), min(len(lines), b + CONTEXT_WINDOW_LINES)) for a, b in cites.get(rel, [])] or [(1, 120)]
            windows = [{"start_line": a, "text": "\n".join(lines[a - 1:b])} for a, b in sorted(set(spans))[:4]]
            entry = {"path": rel, "mode": "windows", "lines": len(lines), "windows": windows}
        size = len(str(entry).encode("utf-8"))
        if used + size > total_bytes:
            out.append({"path": rel, "mode": "omitted", "lines": len(lines), "note": "context budget reached: read_range it"})
            continue
        used += size
        out.append(entry)
    return out


def _python_errors(ws, paths: Sequence[str]) -> List[str]:
    errs = []
    for rel in paths:
        try:
            path = ws_mod.contained_path(ws.path, rel)
            if not (rel.endswith(".py") and path.is_file()):
                continue
            ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except ws_mod.WorkspaceError:
            continue
        except SyntaxError as exc:
            errs.append(f"{rel}:{exc.lineno}: {exc.msg}")
    return errs


def attempt_workspace_id(case_id, attempt: int) -> uuid.UUID:
    return uuid.uuid5(_NS, f"{case_id}:attempt:{attempt}")


def open_attempt_workspace(society_settings: SocietySettings, case_id, attempt: int, *, base_ref: str = "HEAD", fresh: bool = True) -> ws_mod.Workspace:
    ws = ws_mod.ensure_workspace(society_settings, attempt_workspace_id(case_id, attempt), base_ref=base_ref)
    if fresh:
        reset_to_base(ws)
    return ws


@dataclass
class TargetedTestRun:
    passed: bool
    returncode: int
    failures: List[Dict[str, str]] = field(default_factory=list)
    tail: str = ""
    targets: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"passed": self.passed, "returncode": self.returncode, "failures": self.failures[:20], "tail": self.tail[-3000:], "targets": self.targets}


def run_targeted_tests(ws: ws_mod.Workspace, targets: Sequence[str], *, timeout: int) -> TargetedTestRun:
    """pytest with an argv list, in the worktree, scrubbed environment."""
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x", "-rfE", *targets]
    try:
        proc = subprocess.run(argv, cwd=str(ws.path), env=qa_mod.scrubbed_env(ws), capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return TargetedTestRun(False, -1, [{"test": "*", "reason": f"timed out after {timeout}s"}], "", list(targets))
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    failures = []
    for line in out.splitlines():
        m = _FAILED_RE.match(line.strip())
        if m:
            failures.append({"test": m.group(2)[:200], "reason": (m.group(3) or "")[:300]})
    return TargetedTestRun(proc.returncode == 0, proc.returncode, failures, out[-4000:], list(targets))


@dataclass
class AttemptState:
    ws: ws_mod.Workspace
    files_allowed: List[str]
    test_targets: List[str]
    max_test_runs: int
    test_timeout: int
    page_bytes: int
    patches_applied: int = 0
    test_runs: int = 0
    last_test: Optional[TargetedTestRun] = None
    patch_digests: List[str] = field(default_factory=list)


def builder_tools(state: AttemptState) -> Dict[str, Any]:
    """Tools bound to one attempt's worktree. Read tools see the worktree
    (including the Builder's own uncommitted edits)."""
    repo = RepoTools(state.ws.path, page_bytes=state.page_bytes)

    def apply_patch(args: Dict[str, Any]) -> Dict[str, Any]:
        raw = dict(args)
        raw.setdefault("base_sha", state.ws.base_sha)
        try:
            patch = PatchSet.model_validate(raw)
            before = {f.path: _snapshot(state.ws, f.path) for f in patch.files}
            applied = apply_patchset(state.ws, patch, files_allowed=state.files_allowed)
        except PatchError as exc:
            return {"error": str(exc), "code": exc.code, "applied": False}
        except ValueError as exc:  # pydantic ValidationError is a ValueError
            return {"error": str(exc)[:600], "code": "invalid_patch", "applied": False}
        errs = _python_errors(state.ws, applied.written)
        if errs:  # refused inside the try: the model sees why, the worktree is unchanged
            for rel, text in before.items():
                _restore(state.ws, rel, text)
            return {"error": "the patch leaves Python that does not parse; nothing was written", "code": "syntax_error", "syntax_errors": errs[:5], "applied": False}
        state.patches_applied += 1
        state.patch_digests.append(applied.digest)
        return {"applied": True, "written": applied.written, "deleted": applied.deleted}

    def run_tests(args: Dict[str, Any]) -> Dict[str, Any]:
        targets = args.get("targets") or state.test_targets
        if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
            return {"error": "targets must be a list of pytest node ids"}
        allowed = set(state.test_targets) | {f for f in state.files_allowed if "/tests/" in f"/{f}" or f.startswith("tests/")}
        bad = [t for t in targets if t.split("::")[0] not in {a.split("::")[0] for a in allowed}]
        if bad:
            return {"error": f"only the plan's tests (or tests in files_allowed) may run: {bad[:5]}"}
        if state.test_runs >= state.max_test_runs:
            return {"error": f"test-run budget ({state.max_test_runs}) for this attempt is used up; submit or answer needs_rescope"}
        state.test_runs += 1
        state.last_test = run_targeted_tests(state.ws, targets, timeout=state.test_timeout)
        return state.last_test.as_dict()

    def read_diff(args: Dict[str, Any]) -> Dict[str, Any]:
        diff = ws_mod._git(["diff", state.ws.base_sha], cwd=state.ws.path)
        return page_text("(diff)", diff, start_line=int(args.get("start_line") or 1), max_bytes=state.page_bytes).as_dict()

    def reset_attempt(args: Dict[str, Any]) -> Dict[str, Any]:
        reset_to_base(state.ws)
        state.patches_applied = 0
        return {"reset": True, "base_sha": state.ws.base_sha}

    def _wrap(fn):
        def inner(args):
            try:
                return fn(args)
            except RepoToolError as exc:
                return {"error": str(exc)}
        return inner

    return {
        "read_range": _wrap(lambda a: repo.read_range(str(a.get("path", "")), int(a.get("start_line") or 1))),
        "find_symbol": _wrap(lambda a: repo.find_symbol(str(a.get("name", "")))),
        "list_definitions": _wrap(lambda a: repo.list_definitions(str(a.get("path", "")))),
        "list_references": _wrap(lambda a: repo.list_references(str(a.get("name", "")))),
        "route_map": _wrap(lambda a: repo.route_map(str(a.get("app_path") or "services/dashboard/app"))),
        "template_refs": _wrap(lambda a: repo.template_refs(str(a.get("templates") or "services/dashboard/app/templates"), str(a.get("app_path") or "services/dashboard/app"))),
        "test_ownership": _wrap(lambda a: repo.test_ownership(str(a.get("path", "")))),
        "search": _wrap(lambda a: repo.search(str(a.get("literal", "")))),
        "apply_patch": apply_patch,
        "run_tests": run_tests,
        "read_diff": read_diff,
        "reset_attempt": reset_attempt,
    }


def _snapshot(ws, rel: str) -> Optional[str]:
    try:
        path = ws_mod.contained_path(ws.path, rel)
    except ws_mod.WorkspaceError:
        return None
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _restore(ws, rel: str, text: Optional[str]) -> None:
    path = ws_mod.contained_path(ws.path, rel)
    if text is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(text, encoding="utf-8")


def submit_check(state: AttemptState):
    """Checked when the Builder submits, INSIDE the try: a no-op or
    whitespace-only worktree, or Python that does not parse, is refused with a
    structural error the model sees (not discovered after the try ended)."""

    def check(_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        status = ws_mod._git(["status", "--porcelain", "--untracked-files=all"], cwd=state.ws.path)
        paths = [ln[3:].strip() for ln in status.splitlines() if ln.strip()]
        if not paths:
            return {"error": "the worktree is unchanged: apply_patch before submitting, or answer needs_rescope", "code": "empty_patch"}
        real = ws_mod._git(["diff", "--ignore-all-space", "--ignore-blank-lines", state.ws.base_sha], cwd=state.ws.path)
        new_files = [ln for ln in status.splitlines() if ln.startswith("??")]
        if not new_files and not any(ln.startswith(("+", "-")) and not ln.startswith(("+++", "---")) for ln in real.splitlines()):
            return {"error": "the change is whitespace/blank lines only: make the substantive repair", "code": "format_only"}
        errs = _python_errors(state.ws, paths)
        if errs:
            return {"error": "Python in the attempt does not parse", "code": "syntax_error", "syntax_errors": errs[:5]}
        return None

    return check


def read_tools(root, *, page_bytes: int) -> Dict[str, Any]:
    """Read-only tools over a checkout (diagnosis / design / review)."""
    repo = RepoTools(root, page_bytes=page_bytes)

    def _wrap(fn):
        def inner(args):
            try:
                return fn(args)
            except RepoToolError as exc:
                return {"error": str(exc)}
        return inner

    return {
        "read_range": _wrap(lambda a: repo.read_range(str(a.get("path", "")), int(a.get("start_line") or 1))),
        "find_symbol": _wrap(lambda a: repo.find_symbol(str(a.get("name", "")))),
        "list_definitions": _wrap(lambda a: repo.list_definitions(str(a.get("path", "")))),
        "list_references": _wrap(lambda a: repo.list_references(str(a.get("name", "")))),
        "route_map": _wrap(lambda a: repo.route_map(str(a.get("app_path") or "services/dashboard/app"))),
        "template_refs": _wrap(lambda a: repo.template_refs(str(a.get("templates") or "services/dashboard/app/templates"), str(a.get("app_path") or "services/dashboard/app"))),
        "test_ownership": _wrap(lambda a: repo.test_ownership(str(a.get("path", "")))),
        "search": _wrap(lambda a: repo.search(str(a.get("literal", "")))),
    }


@dataclass
class Verification:
    passed: bool
    qa: Dict[str, Any]
    risk: Dict[str, Any]
    changed: List[str]
    head_sha: str
    diff_digest: str
    diff_lines: int
    feedback: List[str]


def finalize_attempt(ws: ws_mod.Workspace, message: str) -> Dict[str, Any]:
    head = ws_mod.commit_all(ws, message)
    changed = ws_mod.changed_files(ws)
    d_hash, d_lines = ws_mod.diff_identity(ws)
    return {"head_sha": head, "changed": changed, "diff_digest": d_hash, "diff_lines": d_lines, "format_only": ws_mod.is_format_only(ws) if changed else False}


def verify(society_settings: SocietySettings, settings: MaintenanceSettings, ws: ws_mod.Workspace, *, files_allowed: Sequence[str], acceptance_tests: Sequence[str]) -> Verification:
    """Deterministic verification of the committed attempt head."""
    changed = ws_mod.changed_files(ws)
    diff = ws_mod.diff_text(ws, max_chars=400_000)
    spec = {"files_allowed": list(files_allowed), "acceptance_tests": list(acceptance_tests), "kind": "maintenance"}
    report = qa_mod.evaluate_candidate(society_settings, ws, spec, changed)
    risk = policy_mod.classify_patch(changed, diff, settings=settings)
    d_hash, d_lines = ws_mod.diff_identity(ws)
    feedback = list(report.failures)
    if not changed:
        feedback.append("empty change")
    elif ws_mod.is_format_only(ws):
        feedback.append("format-only change (anti-busywork)")
    passed = report.passed and bool(changed) and not ws_mod.is_format_only(ws)
    qa = report.to_dict()
    qa["head_sha"] = ws_mod.head_sha(ws)
    qa["static_findings"] = getattr(report, "static_findings", [])
    return Verification(passed, qa, risk.to_dict(), changed, qa["head_sha"], d_hash, d_lines, feedback[:20])


__all__ = [
    "attempt_workspace_id",
    "open_attempt_workspace",
    "run_targeted_tests",
    "AttemptState",
    "builder_tools",
    "submit_check",
    "target_file_context",
    "read_tools",
    "finalize_attempt",
    "verify",
    "Verification",
    "TargetedTestRun",
]
