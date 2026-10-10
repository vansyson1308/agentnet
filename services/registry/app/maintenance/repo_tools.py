"""Read-only, bounded, structural repository intelligence for repair
activities (ADR-0010 D8). Returns UNTRUSTED data (it is repository content a
candidate may have changed) and never runs a shell.

Context is PAGED, never silently truncated: every read of something that
does not fit says ``total_bytes``, ``total_lines``, the shown line range,
``truncated`` and the ``next_line`` cursor. The invariant is generic
(:func:`page_text`), not per file.

Tools:

* ``read_range``          -- a page of whole lines from ``start_line``;
* ``find_symbol``         -- Python definitions by name (ast, not regex);
* ``list_definitions``    -- a module's classes/functions with line numbers;
* ``list_references``     -- word-boundary references across source files;
* ``route_map``           -- Flask/FastAPI route decorators -> endpoint names;
* ``template_refs``       -- ``url_for('<endpoint>')`` uses in Jinja templates,
                             each marked registered / NOT registered;
* ``test_ownership``      -- test files that import or name a module;
* ``search``              -- a literal search, bounded.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
from dataclasses import asdict, dataclass
from typing import Dict, Iterator, List, Optional, Sequence

from ..society.risk import is_never_readable

SOURCE_SUFFIXES = (".py", ".html", ".jinja", ".js", ".css", ".json", ".md", ".txt", ".toml", ".ini", ".yml", ".yaml")
DEFAULT_ROOTS = ("services", "sdk", "examples", "tests", "docs", "scripts", "deploy")
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache", ".mypy_cache", "legacy"}
#: Bench holdout tasks: scored only by the controller, never in any model's
#: context -- no read, search or reference tool may return them.
HOLDOUT_PREFIXES = ("scripts/bench/holdout",)
MAX_FILES_SCANNED = 4000
MAX_RESULTS = 60


class RepoToolError(ValueError):
    pass


def is_holdout(rel: str) -> bool:
    return str(rel).lstrip("./").startswith(HOLDOUT_PREFIXES)


@dataclass
class Page:
    path: str
    total_bytes: int
    total_lines: int
    start_line: int
    end_line: int
    truncated: bool
    next_line: Optional[int]
    content: str

    def as_dict(self) -> dict:
        return asdict(self)


def page_text(path: str, text: str, *, start_line: int = 1, max_bytes: int = 12000, numbered: bool = False) -> Page:
    """Whole lines from ``start_line`` until ``max_bytes``. Never cuts a line
    in half (a single line longer than the page is shown alone and marked).
    ``numbered`` prefixes each shown line with ``N| `` (counted in the page)."""
    lines = text.splitlines(keepends=True)
    total = len(lines)
    start = max(1, int(start_line))
    if start > max(total, 1):
        return Page(path, len(text.encode("utf-8")), total, start, start - 1, False, None, "")
    out: List[str] = []
    size = 0
    i = start - 1
    while i < total:
        line = f"{i + 1}| {lines[i]}" if numbered else lines[i]
        b = len(line.encode("utf-8"))
        if out and size + b > max_bytes:
            break
        out.append(line)
        size += b
        i += 1
        if size >= max_bytes:
            break
    end = start + len(out) - 1
    truncated = end < total
    return Page(path, len(text.encode("utf-8")), total, start, end, truncated, (end + 1) if truncated else None, "".join(out))


class RepoTools:
    def __init__(self, root: pathlib.Path, *, roots: Sequence[str] = DEFAULT_ROOTS, page_bytes: int = 12000):
        self.root = pathlib.Path(root).resolve()
        self.roots = tuple(roots)
        self.page_bytes = page_bytes

    # ── helpers ─────────────────────────────────────────────────────────
    def _resolve(self, rel: str) -> pathlib.Path:
        if not rel or rel.startswith(("/", "~")) or "\\" in rel or "\0" in rel or ".." in rel.split("/"):
            raise RepoToolError(f"path not allowed: {rel!r}")
        if is_never_readable(rel) or is_holdout(rel):
            raise RepoToolError(f"{rel} is not readable (secret material / generated / bench holdout)")
        p = (self.root / rel).resolve()
        if self.root not in p.parents and p != self.root:
            raise RepoToolError(f"path escapes the repository: {rel!r}")
        return p

    def _files(self, suffixes: Sequence[str] = SOURCE_SUFFIXES) -> Iterator[pathlib.Path]:
        n = 0
        for r in self.roots:
            base = self.root / r
            if not base.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
                for f in sorted(filenames):
                    if not f.endswith(tuple(suffixes)):
                        continue
                    p = pathlib.Path(dirpath) / f
                    rel = p.relative_to(self.root).as_posix()
                    if is_never_readable(rel) or is_holdout(rel):
                        continue
                    n += 1
                    if n > MAX_FILES_SCANNED:
                        return
                    yield p

    def _rel(self, p: pathlib.Path) -> str:
        return p.relative_to(self.root).as_posix()

    # ── tools ───────────────────────────────────────────────────────────
    def read_range(self, path: str, start_line: int = 1, max_bytes: Optional[int] = None, *, numbered: bool = False) -> dict:
        p = self._resolve(path)
        if not p.is_file():
            raise RepoToolError(f"{path} does not exist")
        text = p.read_text(encoding="utf-8", errors="replace")
        page = page_text(path, text, start_line=start_line, max_bytes=min(max_bytes or self.page_bytes, self.page_bytes), numbered=numbered).as_dict()
        if numbered:
            page["note"] = "each line starts with its line number and '| '; that prefix is NOT file text (never put it in old/anchor)"
        return page

    def list_definitions(self, path: str) -> dict:
        p = self._resolve(path)
        if not path.endswith(".py") or not p.is_file():
            raise RepoToolError("list_definitions needs an existing .py file")
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=path)
        out = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.append({"name": node.name, "kind": type(node).__name__.replace("Def", "").lower(), "line": node.lineno, "end_line": getattr(node, "end_lineno", node.lineno)})
                if isinstance(node, ast.ClassDef):
                    for sub in node.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            out.append({"name": f"{node.name}.{sub.name}", "kind": "method", "line": sub.lineno, "end_line": getattr(sub, "end_lineno", sub.lineno)})
        return {"path": path, "definitions": out[:400], "truncated": len(out) > 400}

    def find_symbol(self, name: str) -> dict:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,80}", name or ""):
            raise RepoToolError("symbol must be an identifier")
        hits = []
        for p in self._files((".py",)):
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, ValueError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                    hits.append({"path": self._rel(p), "line": node.lineno, "end_line": getattr(node, "end_lineno", node.lineno), "kind": type(node).__name__.replace("Def", "").lower()})
                elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                    hits.append({"path": self._rel(p), "line": node.lineno, "end_line": getattr(node, "end_lineno", node.lineno), "kind": "assignment"})
            if len(hits) >= MAX_RESULTS:
                break
        return {"symbol": name, "definitions": hits[:MAX_RESULTS], "truncated": len(hits) >= MAX_RESULTS}

    def list_references(self, name: str) -> dict:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,120}", name or ""):
            raise RepoToolError("reference must be an identifier")
        pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
        hits = []
        for p in self._files((".py", ".html", ".jinja", ".js")):
            try:
                for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if pat.search(line):
                        hits.append({"path": self._rel(p), "line": i, "text": line.strip()[:200]})
                        if len(hits) >= MAX_RESULTS:
                            break
            except OSError:
                continue
            if len(hits) >= MAX_RESULTS:
                break
        return {"name": name, "references": hits, "truncated": len(hits) >= MAX_RESULTS}

    def route_map(self, app_path: str = "services/dashboard/app") -> dict:
        """Routes registered by decorators in a package: ``@app.route``,
        ``@bp.route``, ``@router.get`` ... Endpoint = function name (Flask's
        default), or the explicit ``endpoint=`` keyword."""
        base = self._resolve(app_path)
        routes = []
        files = [base] if base.is_file() else sorted(base.rglob("*.py"))
        for p in files:
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, ValueError):
                routes.append({"path": self._rel(p), "error": "does not parse"})
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in node.decorator_list:
                    if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in ("route", "get", "post", "put", "patch", "delete")):
                        continue
                    rule = dec.args[0].value if dec.args and isinstance(dec.args[0], ast.Constant) else None
                    endpoint = node.name
                    methods = [dec.func.attr.upper()] if dec.func.attr != "route" else ["GET"]
                    for kw in dec.keywords:
                        if kw.arg == "endpoint" and isinstance(kw.value, ast.Constant):
                            endpoint = kw.value.value
                        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                            methods = [e.value for e in kw.value.elts if isinstance(e, ast.Constant)]
                    routes.append({"endpoint": endpoint, "rule": rule, "methods": methods, "path": self._rel(p), "line": node.lineno})
        return {"app": app_path, "routes": routes[:500], "truncated": len(routes) > 500}

    def template_refs(self, templates: str = "services/dashboard/app/templates", app_path: str = "services/dashboard/app") -> dict:
        registered = {r.get("endpoint") for r in self.route_map(app_path)["routes"]} | {"static"}
        base = self._resolve(templates)
        pat = re.compile(r"url_for\(\s*['\"]([A-Za-z0-9_.]+)['\"]")
        refs = []
        for p in sorted(base.rglob("*.html")):
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                for m in pat.finditer(line):
                    refs.append({"template": self._rel(p), "line": i, "endpoint": m.group(1), "registered": m.group(1) in registered})
        missing = sorted({r["endpoint"] for r in refs if not r["registered"]})
        return {"refs": refs[:600], "truncated": len(refs) > 600, "unregistered_endpoints": missing}

    def test_ownership(self, path: str) -> dict:
        p = self._resolve(path)
        stem = p.stem
        module = path[:-3].replace("/", ".") if path.endswith(".py") else None
        tails = {stem}
        if module:
            parts = module.split(".")
            tails.update(".".join(parts[i:]) for i in range(len(parts)))
        pat = re.compile(r"(?:import|from)\s+([A-Za-z0-9_.]+)")
        owners = []
        for f in self._files((".py",)):
            rel = self._rel(f)
            if "/tests/" not in f"/{rel}" or not f.name.startswith("test_"):
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            mods = set(pat.findall(text))
            if any(m.endswith(t) or m.split(".")[-1] == stem for m in mods for t in tails) or os.path.basename(path) in text:
                owners.append(rel)
            if len(owners) >= MAX_RESULTS:
                break
        return {"path": path, "tests": owners, "truncated": len(owners) >= MAX_RESULTS}

    def search(self, literal: str) -> dict:
        if not literal or len(literal) > 200 or "\n" in literal:
            raise RepoToolError("search needs a single-line literal of at most 200 characters")
        hits = []
        for p in self._files():
            try:
                for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if literal in line:
                        hits.append({"path": self._rel(p), "line": i, "text": line.strip()[:200]})
                        if len(hits) >= MAX_RESULTS:
                            return {"literal": literal, "matches": hits, "truncated": True}
            except OSError:
                continue
        return {"literal": literal, "matches": hits, "truncated": False}


def tool_names() -> Dict[str, str]:
    return {
        "read_range": "args {path, start_line?} -> a page of whole lines with total_lines, truncated and next_line",
        "find_symbol": "args {name} -> Python definitions (path, line, end_line)",
        "list_definitions": "args {path} -> classes/functions of a .py file with line ranges",
        "list_references": "args {name} -> word-boundary references (path, line, text)",
        "route_map": "args {app_path?} -> registered routes (endpoint, rule, methods, path, line)",
        "template_refs": "args {templates?, app_path?} -> url_for uses in templates and which endpoints are NOT registered",
        "test_ownership": "args {path} -> test files that import or name the module",
        "search": "args {literal} -> literal matches (path, line, text)",
    }


__all__ = ["Page", "page_text", "RepoTools", "RepoToolError", "tool_names"]
