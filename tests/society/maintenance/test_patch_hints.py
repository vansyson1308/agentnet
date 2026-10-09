"""A refused patch tells the model where it went wrong (live bench, 2026-10-09:
deepseek-flash re-sent the same wrong text after a bare no_match and never
recovered from a bare syntax_error line number)."""

from __future__ import annotations

import subprocess
import uuid

from services.registry.app.maintenance import harness as h
from services.registry.app.society.engineering import workspace as ws_mod

SRC = "def area(w, h):\n    if w < 0:\n        raise ValueError('w')\n    return w * h\n"


def _state(tmp_path):
    (tmp_path / "geo.py").write_text(SRC)
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()
    ws = ws_mod.Workspace(uuid.uuid4(), tmp_path, "main", head, tmp_path)
    return h.AttemptState(ws=ws, files_allowed=["geo.py"], test_targets=[], max_test_runs=1, test_timeout=30, page_bytes=4000)


def _patch(old, new):
    return {"files": [{"path": "geo.py", "operations": [{"op": "replace_exact", "old": old, "new": new}]}]}


def test_no_match_returns_the_nearest_numbered_lines(tmp_path):
    out = h.builder_tools(_state(tmp_path))["apply_patch"](_patch("    if w <= 0:\n        raise ValueError('w')", "    if w < 0 or h < 0:\n        raise ValueError('w')"))
    assert out["code"] == "no_match" and not out["applied"]
    (hint,) = out["nearest"]
    assert hint["path"] == "geo.py" and hint["nearest_line"] == 2 and "    2|     if w < 0:" in hint["file_text"]


def test_a_syntax_refusal_shows_the_lines_around_the_error(tmp_path):
    state = _state(tmp_path)
    out = h.builder_tools(state)["apply_patch"](_patch("    return w * h", "    return (w * h"))
    assert out["code"] == "syntax_error" and (tmp_path / "geo.py").read_text() == SRC
    assert out["syntax_errors"][0].startswith("geo.py:") and "| " in out["syntax_errors"][0] and "return (w * h" in out["syntax_errors"][0]
