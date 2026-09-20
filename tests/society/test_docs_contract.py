"""The docs-candidate contract is ONE object, enforced at design time.

A live Architect specced an edit to an existing document while the trusted
acceptance test requires one NEW file under the candidate directory. Nothing
rejected it: the candidate was created, the Builder produced an empty diff, and
the busywork guard rejected it three steps later saying only "no-op change".
"""

from __future__ import annotations

import pathlib

import pytest

from services.registry.app.society import context as ctx_mod
from services.registry.app.society.engineering import docs_contract as dc

REPO = pathlib.Path(__file__).resolve().parent.parent.parent


def _spec(**over):
    base = {
        "kind": "docs",
        "files_allowed": [dc.DOCS_CANDIDATE_DIR + "a-slug.md"],
        "acceptance_tests": [dc.DOCS_ACCEPTANCE_TEST],
        "expected_effect": "the stale claim stops misleading readers",
    }
    base.update(over)
    return base


def test_a_valid_docs_spec_passes():
    assert dc.validate_docs_spec(_spec(), repo_root=REPO) == []


@pytest.mark.parametrize(
    "over,code",
    [
        ({"files_allowed": ["docs/SOCIETY_LIVE_PROOF.md"]}, "docs_candidate_must_create_new_file"),
        ({"files_allowed": ["docs/elsewhere/x.md"]}, "docs_candidate_must_create_new_file"),
        ({"files_allowed": [dc.DOCS_CANDIDATE_DIR + "x.txt"]}, "docs_candidate_must_create_new_file"),
        ({"files_allowed": [dc.DOCS_CANDIDATE_DIR + "a.md", dc.DOCS_CANDIDATE_DIR + "b.md"]}, "docs_candidate_is_exactly_one_file"),
        ({"acceptance_tests": []}, "docs_candidate_acceptance_test_required"),
        ({"acceptance_tests": ["tests/test_money_invariants.py"]}, "docs_candidate_acceptance_test_required"),
        ({"expected_effect": "  "}, "expected_effect_required"),
    ],
)
def test_contract_violations_are_named_not_guessed(over, code):
    errors = dc.validate_docs_spec(_spec(**over), repo_root=REPO)
    assert code in {e.code for e in errors}, [e.as_dict() for e in errors]
    for e in errors:
        assert e.field and e.expected, "every error must name its field and what was expected"


def test_an_existing_candidate_file_may_not_be_overwritten(tmp_path):
    rel = dc.DOCS_CANDIDATE_DIR + "already-there.md"
    (tmp_path / dc.DOCS_CANDIDATE_DIR).mkdir(parents=True)
    (tmp_path / rel).write_text("# there", encoding="utf-8")
    codes = {e.code for e in dc.validate_docs_spec(_spec(files_allowed=[rel]), repo_root=tmp_path)}
    assert "docs_candidate_must_not_overwrite" in codes
    # …and the same spec is fine against a base that does not have the file
    assert dc.validate_docs_spec(_spec(files_allowed=[rel]), repo_root=REPO) == []


def test_the_contract_does_not_dictate_the_answer():
    """Trusted code defines the boundary; the Architect still chooses."""
    for slug in ("anything.md", "a-different-idea.md", "2026-09-20-notes.md"):
        assert dc.validate_docs_spec(_spec(files_allowed=[dc.DOCS_CANDIDATE_DIR + slug]), repo_root=REPO) == []


# ── the single-source-of-truth regression ─────────────────────────────


def test_qa_convention_and_design_time_validation_cannot_drift():
    """Changing the QA convention without changing design-time validation must
    fail CI. Both sides read these constants, so this test fails the moment a
    third copy appears anywhere."""
    import ast

    path = REPO / dc.DOCS_ACCEPTANCE_TEST
    acceptance = path.read_text(encoding="utf-8")
    # QA runs that file standalone inside a bare Builder worktree, so it cannot
    # import the contract. It restates it — and THIS assertion is what keeps the
    # two halves one contract: parse its literals and compare them to the source.
    tree = ast.parse(acceptance)
    literals = {
        t.id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name) and t.id in {"REQUIRED_SECTIONS"}
    }
    assert literals.get("REQUIRED_SECTIONS") == tuple(dc.DOCS_REQUIRED_SECTIONS), (
        "the QA acceptance test and the design-time contract disagree about the "
        "required sections; change engineering/docs_contract.py and this file together"
    )
    for segment in dc.DOCS_CANDIDATE_DIR.strip("/").split("/"):
        assert f'"{segment}"' in acceptance, (
            f"the acceptance test must look in the directory the contract names (missing {segment!r})"
        )
    # the prompt the Architect reads is generated from the same object
    conv = ctx_mod.engineering_conventions(ctx_mod.SocietySettings())["docs_candidate"]
    assert conv == dc.conventions_line()
    assert dc.DOCS_CANDIDATE_DIR in conv and dc.DOCS_ACCEPTANCE_TEST in conv
    for section in dc.DOCS_REQUIRED_SECTIONS:
        assert section in conv and section in acceptance


def test_context_reexports_stay_identical_to_the_contract():
    assert ctx_mod.DOCS_CANDIDATE_DIR is dc.DOCS_CANDIDATE_DIR
    assert ctx_mod.DOCS_ACCEPTANCE_TEST is dc.DOCS_ACCEPTANCE_TEST
    assert ctx_mod.DOCS_REQUIRED_SECTIONS is dc.DOCS_REQUIRED_SECTIONS


# ── the design-time gate, and its ONE corrective turn ──────────────────


def test_the_executor_refuses_a_contract_breaking_docs_spec_before_creating_a_candidate():
    import inspect

    from services.registry.app.society import executor as ex

    req = inspect.getsource(ex._request_code_change)
    gate = inspect.getsource(ex._enforce_docs_contract)
    # the gate runs BEFORE the candidate row is created
    assert req.index('_enforce_docs_contract') < req.index("cand = CodeCandidate(")
    assert 'spec.get("kind") == "docs"' in req
    # structured, machine-readable errors reach the Architect
    assert "errors" in gate and "e.as_dict()" in gate
    assert "CODE_CHANGE_SPEC_REJECTED" in gate
    # the "new file" rule is judged against the TRUSTED base, never a worktree
    assert "ctx.settings.repo_root" in gate


def test_the_architect_gets_exactly_one_corrective_turn():
    """A model that cannot satisfy the contract must not be able to spin on it."""
    import inspect

    from services.registry.app.society import executor as ex

    gate = inspect.getsource(ex._enforce_docs_contract)
    assert "already == 0" in gate, "the wake is emitted only when none has been emitted yet"
    assert "raise ExecutionError" in gate, "a second violation is still refused"
    # …and the refusal always carries the reason, whether or not it woke anyone
    assert gate.index("already == 0") < gate.index("raise ExecutionError")


def test_the_corrective_event_reaches_the_architect_and_nobody_else():
    from services.registry.app.society.events import EventType
    from services.registry.app.society.roles import DEFAULT_ROLES, ROLE_ARCHITECT, subscriptions_by_event

    routing = subscriptions_by_event(DEFAULT_ROLES)
    assert routing.get(EventType.CODE_CHANGE_SPEC_REJECTED) == [ROLE_ARCHITECT]
