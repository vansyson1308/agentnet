"""
Acceptance test executed by Society_QA inside a Builder worktree.

A documentation candidate lives in ``docs/society/candidates/<slug>.md``
and must have exactly this structure so it can be verified mechanically:

* an H1 title on the first non-empty line,
* the sections ``## Problem``, ``## Proposed change``, ``## Evidence``,
  ``## Verification`` — each with non-empty prose,
* no secret-looking strings.

Runs as part of the normal suite too (it validates every committed
candidate doc), and passes trivially when the directory is empty.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent.parent
CANDIDATE_DIR = REPO / "docs" / "society" / "candidates"
REQUIRED_SECTIONS = ("## Problem", "## Proposed change", "## Evidence", "## Verification")
SECRET_RE = re.compile(r"\bsk-[A-Za-z0-9]{20,}\b|-----BEGIN [A-Z ]*PRIVATE KEY-----|password\s*[:=]\s*\S{8,}", re.IGNORECASE)


def _candidate_docs() -> list[pathlib.Path]:
    if not CANDIDATE_DIR.exists():
        return []
    return sorted(p for p in CANDIDATE_DIR.glob("*.md") if p.name != "README.md")


def test_candidate_dir_layout():
    assert CANDIDATE_DIR.parent.exists(), "docs/society must exist"


def _check_doc(doc: pathlib.Path) -> None:
    text = doc.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines and lines[0].startswith("# "), f"{doc.name}: first line must be an H1 title"
    for section in REQUIRED_SECTIONS:
        assert section in text, f"{doc.name}: missing section {section!r}"
        after = text.split(section, 1)[1]
        body = after.split("\n## ", 1)[0].strip()
        assert body, f"{doc.name}: section {section!r} is empty"
    assert not SECRET_RE.search(text), f"{doc.name}: secret-looking string present"


def test_every_candidate_doc_is_well_formed():
    """Loops instead of parametrizing so an empty directory is a pass, not a skip."""
    docs = _candidate_docs()
    for doc in docs:
        _check_doc(doc)


def test_check_doc_rejects_missing_section(tmp_path):
    bad = tmp_path / "bad.md"
    bad.write_text("# T\n\n## Problem\nx\n")
    with pytest.raises(AssertionError):
        _check_doc(bad)
