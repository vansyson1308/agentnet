"""The ONE docs-candidate contract: design time, build time, QA time, prompt.

A live Architect specced an edit to an EXISTING document while the trusted
acceptance test requires one NEW file under the candidate directory. Nothing
rejected that spec: the candidate was created, the Builder produced an empty
diff, and the busywork guard rejected it three steps later with no signal
about what was actually wrong. The machine's design-time contract and its
QA-time contract have to be the same object, or they drift exactly like that.

This module is the object. It imports nothing from the application -- only the
standard library -- so the acceptance test can import it while running inside a
bare Builder worktree, and so a convention change is a change to ONE file.

Trusted code defines the BOUNDARY. It never dictates the answer: the filename,
the title, the prose and the improvement itself stay the Architect's to choose.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence

#: Directory every documentation candidate lives in. One new file, nothing else.
DOCS_CANDIDATE_DIR = "docs/society/candidates/"
#: The trusted acceptance test QA runs against that file.
DOCS_ACCEPTANCE_TEST = "tests/society/acceptance/test_candidate_docs.py"
#: Sections that test requires, in the words it requires them.
DOCS_REQUIRED_SECTIONS = ("## Problem", "## Proposed change", "## Evidence", "## Verification")
#: A docs candidate is exactly one new file; more is a different kind of change.
DOCS_MAX_FILES = 1


@dataclass(frozen=True)
class SpecError:
    """One machine-readable reason a design was refused.

    ``code`` is stable and greppable; ``expected`` is what the Architect has to
    do instead. Prose belongs in ``expected``, never in ``code``.
    """

    field: str
    code: str
    expected: str

    def as_dict(self) -> dict:
        return {"field": self.field, "code": self.code, "expected": self.expected}


def is_candidate_doc(path: str) -> bool:
    p = (path or "").strip()
    return p.startswith(DOCS_CANDIDATE_DIR) and p.endswith(".md") and not p.endswith("/README.md")


def validate_docs_spec(spec: Mapping[str, Any], *, repo_root: Optional[pathlib.Path] = None) -> List[SpecError]:
    """Check a ``kind="docs"`` spec against the contract QA will enforce.

    ``repo_root`` is the TRUSTED base checkout, never a candidate worktree: the
    "new file" rule is about what exists on the base revision. Omit it only
    where the base is genuinely unavailable; the rule is then skipped rather
    than guessed at.
    """
    errors: List[SpecError] = []
    files: Sequence[str] = list(spec.get("files_allowed") or [])
    tests: Sequence[str] = list(spec.get("acceptance_tests") or [])

    if len(files) != DOCS_MAX_FILES:
        errors.append(SpecError("files_allowed", "docs_candidate_is_exactly_one_file", f"exactly {DOCS_MAX_FILES} new file below {DOCS_CANDIDATE_DIR}"))
    for path in files:
        if not is_candidate_doc(path):
            errors.append(SpecError("files_allowed", "docs_candidate_must_create_new_file", f"a new .md file below {DOCS_CANDIDATE_DIR} (not {path!r})"))
            continue
        if repo_root is not None and (pathlib.Path(repo_root) / path).exists():
            errors.append(SpecError("files_allowed", "docs_candidate_must_not_overwrite", f"{path} already exists on the base revision; choose a new filename"))

    if DOCS_ACCEPTANCE_TEST not in tests:
        errors.append(SpecError("acceptance_tests", "docs_candidate_acceptance_test_required", f"must include {DOCS_ACCEPTANCE_TEST}"))
    for t in tests:
        if t != DOCS_ACCEPTANCE_TEST and not t.startswith(DOCS_ACCEPTANCE_TEST + "::"):
            errors.append(SpecError("acceptance_tests", "docs_candidate_acceptance_test_only", f"a docs candidate is verified by {DOCS_ACCEPTANCE_TEST} alone (not {t!r})"))

    if not str(spec.get("expected_effect") or "").strip():
        errors.append(SpecError("expected_effect", "expected_effect_required", "state the effect this document is expected to have"))

    return errors


def conventions_line() -> str:
    """The one sentence the prompt shows an engineering role."""
    return (
        f"kind=docs: ONE NEW file {DOCS_CANDIDATE_DIR}<slug>.md that does not already exist "
        f"— first line an H1 title, then the sections {', '.join(DOCS_REQUIRED_SECTIONS)} each with prose; "
        f"acceptance_tests must be exactly [{DOCS_ACCEPTANCE_TEST}]. Never edit an existing document in a docs candidate."
    )


__all__ = [
    "DOCS_CANDIDATE_DIR",
    "DOCS_ACCEPTANCE_TEST",
    "DOCS_REQUIRED_SECTIONS",
    "DOCS_MAX_FILES",
    "SpecError",
    "is_candidate_doc",
    "validate_docs_spec",
    "conventions_line",
]
