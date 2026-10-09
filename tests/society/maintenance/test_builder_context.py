"""AuthorPatch context and patch feedback (live bench: the never-passing tasks
edit 50-80KB files the model saw only lines 1-120 of, and re-sent the same
wrong text after a bare no_match)."""

from __future__ import annotations

import subprocess

from services.registry.app.maintenance import harness as h
from services.registry.app.maintenance.config import MaintenanceSettings
from services.registry.app.maintenance.repo_tools import RepoTools
from services.registry.app.society.engineering import workspace as ws_mod

FILLER = "".join(f"def filler_{i}(x):\n    # {'padding ' * 12}\n    return x + {i}\n\n\n" for i in range(150))
BIG = (FILLER + "def _render_bound(field):\n    return field['type']\n\n\n"
       + "def schema_doc(fields):\n    return [_render_bound(f) for f in fields]\n\n\n"
       + "class Driver:\n    def step(self, name):\n        if name.startswith('memory:'):\n            return None\n        return name\n")
TEST = ("from app import big\n\n\ndef _doc():\n    return big.schema_doc([{'type': 'string', 'maxLength': 128}])\n\n\n"
        "def test_bounds_are_rendered():\n    assert '128' in _doc()[0]\n")


def _repo(tmp_path):
    for rel, text in {"app/__init__.py": "", "app/big.py": BIG, "tests/test_big.py": TEST}.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    return tmp_path


def test_a_big_target_file_arrives_as_outline_plus_the_windows_the_tests_and_task_point_at(tmp_path):
    root = _repo(tmp_path)
    (entry, tests) = h.target_file_context(root, ["app/big.py"], ["add a read-only 'memory:<role>' step to `Driver`"],
                                           tests=["tests/test_big.py::test_bounds_are_rendered"])
    lines = BIG.splitlines()
    at = {name: lines.index(text) + 1 for name, text in [("render", "def _render_bound(field):"), ("doc", "def schema_doc(fields):"), ("memory", "        if name.startswith('memory:'):")]}
    assert entry["mode"] == "outline+windows" and len(BIG) > h.CONTEXT_WHOLE_FILE_BYTES
    assert f"{at['doc']}-{at['doc'] + 1} def schema_doc" in entry["outline"] and "def Driver.step" in entry["outline"]
    text = "\n".join(w["text"] for w in entry["windows"])
    # the function the test's helper calls, the helper it uses (one level down) and the task's quoted string
    for key in ("render", "doc", "memory"):
        assert f"{at[key]}| {lines[at[key] - 1]}" in text, key
    assert "1| def filler_0(x):" not in text, "no blind first page when the task and tests point somewhere"
    assert tests["mode"] == "acceptance_tests" and tests["tests"][0]["source"].startswith("def test_bounds_are_rendered")
    assert "prefix is NOT file text" in entry["note"]


def test_terms_are_code_shaped_and_apostrophes_are_not_quotes():
    terms = h._terms(["print the payload's key names and the prompt's schema; add an 'intents:<id>' step for READ_REPO_RANGE and evidence.signal"])
    assert terms == ["intents:<id>", "intents:", "READ_REPO_RANGE", "evidence.signal"]


def test_read_range_pages_are_numbered_within_the_page_budget(tmp_path):
    root = _repo(tmp_path)
    page = RepoTools(root, page_bytes=300).read_range("app/big.py", 6, numbered=True)
    assert page["content"].startswith("6| def filler_1(x):\n") and len(page["content"].encode()) <= 300 and page["truncated"]
    assert "prefix is NOT file text" in page["note"]
    assert RepoTools(root).read_range("app/big.py", 1)["content"].startswith("def filler_0(x):"), "plain reads stay plain"


def _state(root):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"], cwd=root, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
    ws = ws_mod.Workspace(None, root, "t", base, root)
    return h.AttemptState(ws=ws, files_allowed=["app/big.py"], test_targets=["tests/test_big.py::test_bounds_are_rendered"], max_test_runs=2, test_timeout=60, page_bytes=12000)


def _patch(old, new):
    return {"files": [{"path": "app/big.py", "operations": [{"op": "replace_exact", "old": old, "new": new}]}]}


def test_a_no_match_shows_the_closest_region_numbered(tmp_path):
    tools = h.builder_tools(_state(_repo(tmp_path)))
    out = tools["apply_patch"](_patch("def schema_doc(field_list):\n    return [_render_bound(f) for f in field_list]", "x = 1"))
    (near,) = out["closest"]
    assert out["code"] == "no_match" and near["path"] == "app/big.py" and near["operation"] == 0
    start = BIG.splitlines().index("def schema_doc(fields):") + 1
    assert f"{start}| def schema_doc(fields):" in near["text"] and len(near["text"].splitlines()) <= 30


def test_a_syntax_refusal_shows_the_would_be_lines_around_the_error(tmp_path):
    tools = h.builder_tools(_state(_repo(tmp_path)))
    out = tools["apply_patch"](_patch("    return [_render_bound(f) for f in fields]", "    return [_render_bound(f) for f in fields"))
    assert out["code"] == "syntax_error" and out["applied"] is False
    shown = out["syntax_errors"][0].splitlines()[1:]
    assert any("return [_render_bound(f) for f in fields" in ln for ln in shown) and 2 <= len(shown) <= 11
    assert "_render_bound(f) for f in fields]" in (tmp_path / "app/big.py").read_text(), "refused patch left the file unchanged"


def test_the_builder_read_budget_default_stays_three(monkeypatch):
    # live bench x3: 12 reads per try fell to pass@1 0.361 (turns spent reading); 3 with this context: 0.778
    monkeypatch.delenv("MAINTENANCE_BUILDER_MAX_READ_CALLS", raising=False)
    assert MaintenanceSettings().builder_max_read_calls == 3
