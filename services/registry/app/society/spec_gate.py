"""Design-time spec gate (deterministic, no model): a design that cannot become a READY candidate
is refused in the review, before any build is paid for.

Staging 2026-10-26: 4 of 4 candidates were REJECTED for faults the design already carried:

* the only acceptance test was a file the Builder had to write -> QA ``no_self_judging``;
* a dashboard template whose view (``services/dashboard/app/main.py``) was outside files_allowed,
  or a template no view renders at all (the change would never reach a page);
* a file on a risky surface (``qa.RISKY_PATH_RE``: wallet, payment, auth, ...) -> the static scan
  fails Security for ANY change there, whatever the reviewer says.

Rules (``check_files`` / ``check_design``):

1. every acceptance test exists on the trusted base, or is a ``test_first`` file -- a new test the
   ARCHITECT writes in the design (the meeting reviews it), committed before the Builder starts;
   an acceptance file is never in files_allowed (the Builder may not write what judges it);
2. a test_first file is new, ``[services/<svc>/]tests/**/test_*.py``, and not in files_allowed;
3. a dashboard template must be rendered (by a view, or through ``extends``/``include`` of a
   rendered one); its view file is in files, or named in ``unchanged`` with the reason;
4. nothing on a risky or never-writable surface (Security cannot pass it).

The test_first content is pinned (sha256) into the spec by the executor from the PERSISTED minutes;
QA exempts only a byte-identical file from the allow-list / self-judging checks (``intact``).
"""

from __future__ import annotations

import hashlib
import pathlib
import re
from typing import Any, Dict, Iterable, List, Sequence

DASHBOARD_APP = "services/dashboard/app"
TEMPLATES = f"{DASHBOARD_APP}/templates/"
TEST_FIRST_RE = re.compile(r"^(?:services/[a-z_]+/)?tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py$")
MAX_TEST_FIRST = 2
_RENDER_RE = re.compile(r"""render_template\(\s*["']([^"']+\.html)["']""")
_REF_RE = re.compile(r"""\{%-?\s*(?:extends|include|import|from)\s+["']([^"']+\.html)["']""")


def _risky(path: str) -> bool:
    from .engineering.qa import RISKY_PATH_RE  # noqa: PLC0415
    from .engineering.workspace import is_protected  # noqa: PLC0415

    return bool(RISKY_PATH_RE.search(path)) or is_protected(path)


def template_views(root: pathlib.Path) -> Dict[str, List[str]]:
    """``templates/<name>`` -> the python files that render it; a template reached through
    extends/include of a rendered one maps to its parent's views."""
    app = root / DASHBOARD_APP
    views: Dict[str, List[str]] = {}
    for py in sorted(app.rglob("*.py")) if app.is_dir() else []:
        rel = py.relative_to(root).as_posix()
        for name in _RENDER_RE.findall(py.read_text(encoding="utf-8", errors="replace")):
            views.setdefault(name, [])
            if rel not in views[name]:
                views[name].append(rel)
    changed = True
    while changed:  # fixpoint over extends/include
        changed = False
        for name, files in list(views.items()):
            tpl = root / TEMPLATES / name
            for ref in _REF_RE.findall(tpl.read_text(encoding="utf-8", errors="replace")) if tpl.is_file() else []:
                if ref not in views:
                    views[ref], changed = list(files), True
    return views


def _unchanged_paths(unchanged: Iterable[str]) -> set:
    return {str(u).split(":", 1)[0].strip() for u in unchanged}


def check_files(root: pathlib.Path, files: Sequence[str], acceptance: Sequence[str], test_first: Sequence[str] = (),
                unchanged: Sequence[str] = ()) -> List[str]:
    """Every problem a READY candidate would hit, as one-line structural reasons (our words, paths)."""
    problems: List[str] = []
    files_set, first = set(files), list(test_first)
    if not acceptance:
        problems.append("no acceptance test: name an existing test (tests/...::test_x) or write one as test_first")
    for test in acceptance:
        path = str(test).split("::")[0]
        if path.startswith("bench:"):
            continue
        if path in files_set:
            problems.append(f"{path} judges the change but is in files: the Builder may not write its own judge -- "
                            "keep an existing test out of files, or move a new one to test_first")
        elif path not in first and not (root / path).is_file():
            problems.append(f"{path} does not exist on main: write it as test_first (the Builder may not)")
    if len(first) > MAX_TEST_FIRST:
        problems.append(f"at most {MAX_TEST_FIRST} test_first files")
    for path in first:
        if not TEST_FIRST_RE.match(path):
            problems.append(f"test_first {path}: must be a new [services/<svc>/]tests/**/test_*.py file")
        elif (root / path).exists():
            problems.append(f"test_first {path} already exists on main: name it in acceptance_tests instead")
        if path in files_set:
            problems.append(f"test_first {path} is in files: the Builder may not edit it")
    views = template_views(root)
    named = files_set | _unchanged_paths(unchanged)
    for path in sorted(files_set):
        if path.startswith(TEMPLATES):
            rendered = views.get(path[len(TEMPLATES):])
            if not rendered:
                problems.append(f"{path} is not rendered by any view: the change would reach no page -- change a rendered "
                                f"template, or add its route to {DASHBOARD_APP}/main.py in files")
            else:
                missing = [v for v in rendered if v not in named]
                if missing:
                    problems.append(f"{path} is rendered by {', '.join(missing)}: add it to files, or name it in unchanged "
                                    "('path: why the view needs no change')")
    for path in sorted(files_set | set(first)):
        if _risky(path):
            problems.append(f"{path} is a risky surface: the Security static scan fails any change there -- design around it")
    return problems


def check_design(root: pathlib.Path, proposal: Dict[str, Any]) -> List[str]:
    first = [str(f.get("path")) for f in proposal.get("test_first") or []]
    return check_files(root, list(proposal.get("files") or []), list(proposal.get("acceptance_tests") or []), first,
                       list(proposal.get("unchanged") or []))


def pin(proposal: Dict[str, Any]) -> List[Dict[str, str]]:
    """The approved design's test_first files, content-addressed (the spec carries them; the model never re-sends them)."""
    return [{"path": str(f["path"]), "content": str(f["content"]), "sha256": hashlib.sha256(str(f["content"]).encode()).hexdigest()}
            for f in proposal.get("test_first") or []]


def intact(ws_path: pathlib.Path, pinned: Sequence[Dict[str, str]]) -> Dict[str, bool]:
    """path -> the worktree file is byte-identical to the pinned test_first content."""
    out = {}
    for f in pinned:
        p = ws_path / f["path"]
        out[f["path"]] = p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == f["sha256"]
    return out


def commit_test_first(ws: Any, pinned: Sequence[Dict[str, str]]) -> str:
    """Back to the candidate's base, write the pinned tests, commit them as the Architect's: the
    sha every build of this candidate starts from (the harness resets to it, never past it)."""
    from ..maintenance.patchset import reset_to_base  # noqa: PLC0415
    from .engineering import workspace as ws_mod  # noqa: PLC0415

    reset_to_base(ws)
    for f in pinned:
        target = ws_mod.contained_path(ws.path, f["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["content"], encoding="utf-8")
    return ws_mod.commit_all(ws, f"test-first: acceptance tests from the approved design ({len(pinned)} file(s))")
