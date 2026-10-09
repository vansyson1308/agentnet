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
import difflib
import hashlib
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
CONTEXT_TOTAL_BYTES = 40_000
CONTEXT_WINDOW_LINES = 40
CONTEXT_HIT_LINES = 12
CONTEXT_MAX_HITS_PER_TERM = 20
CONTEXT_OUTLINE_BYTES = 6_000
CONTEXT_TESTS_BYTES = 8_000
NUMBERED_NOTE = "each line starts with its line number and '| '; that prefix is NOT file text (never put it in old/anchor)"
_TERM_RE = re.compile(r"`([^`\n]{3,80})`|(?<![A-Za-z])'([^'\n]{3,80})'(?![A-Za-z])|\"([^\"\n]{3,80})\"|([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+|[A-Za-z_]*[a-z0-9]_[A-Za-z0-9_]*|[A-Z][A-Z0-9_]{3,}|[a-z]+[A-Z][A-Za-z0-9]*|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*)")


def numbered(lines: Sequence[str], start: int, end: int) -> str:
    """Lines ``start..end`` (1-based, inclusive), each prefixed ``N| ``."""
    return "\n".join(f"{n}| {lines[n - 1]}" for n in range(max(1, start), min(len(lines), end) + 1))


def _terms(texts: Sequence[str]) -> List[str]:
    """Identifiers and quoted strings named by the task/plan (code-shaped words only)."""
    out: List[str] = []
    for text in texts:
        for m in _TERM_RE.finditer(str(text)):
            quoted = m.group(1) or m.group(2) or m.group(3)
            cands = [m.group(4)] if m.group(4) else [quoted, *re.findall(r"[A-Za-z_][\w.]*[:(]|[A-Za-z_]\w*_\w+", quoted)]
            out += [c for c in dict.fromkeys(cands) if c and len(c.strip()) >= 4 and c not in out]
    return out[:40]


def _named_defs(text: str, names: set, *, depth: int = 2) -> List[tuple]:
    """Line spans of the definitions ``names`` refer to, then of the same-file
    definitions those use (``depth`` levels): where a test's behaviour is decided."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    defs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    order, seen, level = [], set(), names & set(defs)
    for _ in range(depth + 1):
        level = sorted(level - seen, key=lambda n: defs[n].lineno)
        order += level
        seen |= set(level)
        level = {getattr(x, "id", None) or getattr(x, "attr", None) for n in level for x in ast.walk(defs[n])} & set(defs)
    return [(defs[n].lineno, min(defs[n].end_lineno, defs[n].lineno + 80)) for n in order]


def _refs(node) -> set:
    return {getattr(x, "id", None) or getattr(x, "attr", None) for x in ast.walk(node)} - {None}


def _outline(rel: str, text: str) -> str:
    """Top-level and class-level definitions (Python) or headings (Markdown), with line ranges."""
    rows: List[str] = []
    if rel.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return ""
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                rows.append(f"{node.lineno}-{node.end_lineno} {'class' if isinstance(node, ast.ClassDef) else 'def'} {node.name}")
                if isinstance(node, ast.ClassDef):
                    rows += [f"{s.lineno}-{s.end_lineno}   def {node.name}.{s.name}" for s in node.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))]
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.end_lineno - node.lineno >= 3:
                names = [t.id for t in (node.targets if isinstance(node, ast.Assign) else [node.target]) if isinstance(t, ast.Name)]
                rows += [f"{node.lineno}-{node.end_lineno} {n} =" for n in names]
    else:
        rows = [f"{i} {ln}" for i, ln in enumerate(text.splitlines(), 1) if ln.startswith("#")]
    out = "\n".join(rows)
    return out if len(out) <= CONTEXT_OUTLINE_BYTES else out[:CONTEXT_OUTLINE_BYTES].rsplit("\n", 1)[0] + "\n... (outline cut)"


def _merge(spans: List[tuple]) -> List[tuple]:
    out: List[tuple] = []
    for a, b in spans:
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _test_sources(root, tests: Sequence[str]) -> tuple:
    """The named acceptance tests' own source (read-only context; tests are
    never in scope), and the names they use -- with the names used by the
    test-module helpers they call."""
    import pathlib  # noqa: PLC0415

    root = pathlib.Path(root).resolve()
    out: List[Dict[str, Any]] = []
    names: set = set()
    used = 0
    for node_id in tests:
        rel, _, name = node_id.partition("::")
        target = (root / rel).resolve()
        if not name or not rel.endswith(".py") or not target.is_file() or root not in target.parents:
            continue
        text = target.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        leaf = name.split("::")[-1].split("[")[0]
        node = funcs.get(leaf) or next((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == leaf), None)
        if node is None:
            continue
        refs = _refs(node)
        names |= refs | set().union(*(_refs(funcs[h]) for h in refs & set(funcs)))
        src = "\n".join(text.splitlines()[node.lineno - 1:node.end_lineno])
        if used + len(src) <= CONTEXT_TESTS_BYTES:
            used += len(src)
            out.append({"test": node_id, "start_line": node.lineno, "source": src})
    return out, names


def target_file_context(root, files_allowed: Sequence[str], citations: Sequence[str], *, whole_file_bytes: int = CONTEXT_WHOLE_FILE_BYTES,
                        total_bytes: int = CONTEXT_TOTAL_BYTES, tests: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """Front-loaded AuthorPatch context. Each non-test target file whole when it
    is small; a larger one as its outline plus line-numbered windows: the lines
    the diagnosis/plan cited (``path:line``) and the lines where identifiers or
    quoted strings named in the task/plan occur (rarest first), else its first
    page. Then the named acceptance tests' source. Bounded; files outside the
    worktree are skipped."""
    import pathlib  # noqa: PLC0415

    root = pathlib.Path(root).resolve()
    cites: Dict[str, List[tuple]] = {}
    for text in citations:
        for m in _CITE_RE.finditer(str(text)):
            a = int(m.group(2))
            cites.setdefault(m.group(1).lstrip("./"), []).append((a, int(m.group(3) or a)))
    terms = _terms(citations)
    srcs, called = _test_sources(root, tests)
    targets = [r for r in files_allowed if "/tests/" not in f"/{r}" and (root / r).resolve().is_file() and root in (root / r).resolve().parents]
    per_file = max(8_000, total_bytes // max(1, len(targets)))
    out: List[Dict[str, Any]] = []
    used = 0
    for rel in targets:
        text = (root / rel).resolve().read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if len(text.encode("utf-8")) <= whole_file_bytes:
            entry: Dict[str, Any] = {"path": rel, "mode": "whole", "lines": len(lines), "content": text}
        else:
            spans = [(max(1, a - CONTEXT_WINDOW_LINES), min(len(lines), b + CONTEXT_WINDOW_LINES)) for a, b in cites.get(rel, [])]
            # the target-file definitions the acceptance tests name
            spans += _named_defs(text, called) if rel.endswith(".py") else []
            hits = [(t, [i for i, ln in enumerate(lines, 1) if t in ln]) for t in terms]
            for _t, rows in sorted((h for h in hits if 0 < len(h[1]) <= CONTEXT_MAX_HITS_PER_TERM), key=lambda h: len(h[1])):
                spans += [(max(1, i - CONTEXT_HIT_LINES), min(len(lines), i + CONTEXT_HIT_LINES)) for i in rows]
            entry = {"path": rel, "mode": "outline+windows", "lines": len(lines), "note": NUMBERED_NOTE, "outline": _outline(rel, text), "windows": []}
            budget = min(per_file, total_bytes - used) - len(entry["outline"]) - 200
            for a, b in _merge(sorted(set(spans))) or [(1, 120)]:
                chunk = numbered(lines, a, b)
                if len(chunk) > budget:
                    continue
                budget -= len(chunk)
                entry["windows"].append({"start_line": a, "end_line": min(b, len(lines)), "text": chunk})
            entry["windows"].sort(key=lambda w: w["start_line"])
        size = len(str(entry).encode("utf-8"))
        if used + size > total_bytes:
            out.append({"path": rel, "mode": "omitted", "lines": len(lines), "outline": _outline(rel, text), "note": "context budget reached: read_range it"})
            continue
        used += size
        out.append(entry)
    if srcs:
        out.append({"mode": "acceptance_tests", "note": "read-only: these tests judge the repair and are not in scope", "tests": srcs})
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
            line = int(exc.lineno or 1)
            errs.append(f"{rel}:{line}: {exc.msg}\n" + numbered(path.read_text(encoding="utf-8").splitlines(), line - 5, line + 5))
    return errs


def _closest(text: str, needle: str, *, max_lines: int = 30) -> Dict[str, Any]:
    """The file region most like ``needle`` (difflib over same-height windows), numbered."""
    lines, want = text.splitlines(), needle.strip("\n").splitlines() or [""]
    height = min(len(want), max_lines)
    probe = "\n".join(ln.strip() for ln in want[:height])
    firsts = difflib.get_close_matches(want[0].strip(), [ln.strip() for ln in lines], n=8, cutoff=0.3)
    starts = {i for i, ln in enumerate(lines) if ln.strip() in firsts} or set(range(0, len(lines), max(1, height // 2)))
    best = max(sorted(starts), key=lambda i: difflib.SequenceMatcher(None, probe, "\n".join(ln.strip() for ln in lines[i:i + height])).ratio(), default=0)
    pad = max(0, (max_lines - height) // 2)
    lo, hi = best + 1 - min(pad, 3), best + height + min(pad, 3)
    return {"start_line": max(1, lo), "text": numbered(lines, lo, hi), "note": NUMBERED_NOTE}


def _nearest(ws, patch: PatchSet) -> List[Dict[str, Any]]:
    """For each exact-text operation whose text is not in its file: the closest
    region of the file (live bench: a bare no_match made the model re-send the
    same wrong text)."""
    out: List[Dict[str, Any]] = []
    for fp in patch.files:
        snap = _snapshot(ws, fp.path)
        for i, op in enumerate(fp.operations):
            needle = op.old if op.op == "replace_exact" else op.anchor
            if snap and needle and needle not in snap:
                out.append({"path": fp.path, "operation": i, **_closest(snap, needle)})
    return out[:3]


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
    tested_digest: Optional[str] = None  # the worktree the last test run judged
    tested_full: bool = False  # ... and whether it ran every acceptance test


def worktree_digest(ws) -> str:
    """Identity of the attempt's current worktree (tracked diff + untracked files)."""
    status = ws_mod._git(["status", "--porcelain", "--untracked-files=all"], cwd=ws.path)
    h = hashlib.sha256(status.encode() + b"\0" + ws_mod._git(["diff", ws.base_sha], cwd=ws.path).encode())
    for ln in status.splitlines():
        if ln.startswith("??"):
            path = ws_mod.contained_path(ws.path, ln[3:].strip())
            h.update(path.read_bytes() if path.is_file() else b"")
    return h.hexdigest()


def _green(state: "AttemptState") -> bool:
    t = state.last_test
    return bool(t and t.passed and state.tested_full and state.tested_digest == worktree_digest(state.ws))


def _verify_now(state: "AttemptState") -> bool:
    """Green on the current worktree -- running every acceptance test now
    (one test run, no model call) when the last run did not judge it."""
    if state.last_test and state.tested_full and state.tested_digest == worktree_digest(state.ws):
        return state.last_test.passed  # this worktree was already judged
    if state.test_runs >= state.max_test_runs or not state.test_targets:
        return False
    state.test_runs += 1
    state.last_test = run_targeted_tests(state.ws, state.test_targets, timeout=state.test_timeout)
    state.tested_digest, state.tested_full = worktree_digest(state.ws), True
    return state.last_test.passed


def builder_tools(state: AttemptState) -> Dict[str, Any]:
    """Tools bound to one attempt's worktree. Read tools see the worktree
    (including the Builder's own uncommitted edits)."""
    repo = RepoTools(state.ws.path, page_bytes=state.page_bytes)

    def apply_patch(args: Dict[str, Any]) -> Dict[str, Any]:
        raw = dict(args)
        raw.setdefault("base_sha", state.ws.base_sha)
        patch = None
        try:
            patch = PatchSet.model_validate(raw)
            before = {f.path: _snapshot(state.ws, f.path) for f in patch.files}
            applied = apply_patchset(state.ws, patch, files_allowed=state.files_allowed)
        except PatchError as exc:
            out = {"error": str(exc), "code": exc.code, "applied": False}
            if exc.code == "no_match" and patch is not None:
                out["closest"] = _nearest(state.ws, patch)
            return out
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
        state.tested_digest, state.tested_full = worktree_digest(state.ws), set(state.test_targets) <= set(targets)
        return {**state.last_test.as_dict(), "test_runs_left": state.max_test_runs - state.test_runs}

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
        "read_range": _wrap(lambda a: repo.read_range(str(a.get("path", "")), int(a.get("start_line") or 1), numbered=True)),
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
    whitespace-only worktree, Python that does not parse, or a worktree whose
    acceptance tests do not all pass on it (failing ids named) is refused
    with a structural error the model sees. A worktree the last test run did
    not judge is tested here first (no model turn). Only once the test-run
    budget is spent may an unverified worktree be submitted, flagged
    ``tests_unverified``."""

    def check(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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
        if _verify_now(state):
            return None
        if state.test_runs >= state.max_test_runs or not state.test_targets:
            result["tests_unverified"] = True  # no run left (or nothing to run): QA decides
            return None
        t = state.last_test
        return {"error": "acceptance tests fail on the current worktree: fix them, then submit", "code": "tests_failing",
                "failing": [f["test"] for f in t.failures][:10] or state.test_targets[:10], "failures": t.failures[:10], "tail": t.tail[-1500:],
                "test_runs_left": state.max_test_runs - state.test_runs}

    return check


def submit_if_green(state: AttemptState):
    """After the try: one that ran out of turns with a changed worktree whose
    acceptance tests all pass on it (the last run, or one run now) is
    submitted deterministically (no model call). Anything else is left as it
    ended."""
    check = submit_check(state)

    def finish(res):
        if res.ok or res.error_class != "turn_budget" or res.rescope is not None or check({}) is not None or not _green(state):
            return res
        res.ok, res.error_class, res.error = True, None, None
        res.output = {"summary": f"submitted by the harness: the turn budget ran out with all acceptance tests passing on this worktree ({len(state.test_targets)} tests)",
                      "auto_submitted": True}
        res.turn_log.append({"turn": res.turns, "action": "auto_submit"})
        return res

    return finish


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
    "submit_if_green",
    "worktree_digest",
    "read_tools",
    "finalize_attempt",
    "verify",
    "Verification",
    "TargetedTestRun",
]
