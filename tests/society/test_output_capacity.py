"""Output capacity is not permission.

A live Builder's SUBMIT_CODE_CANDIDATE carries whole file contents in
edits[].content. At 1200 output tokens the JSON was cut off mid-object
(finish_reason=length) and the run went DEAD. Raising the ceiling lets a
legitimate candidate fit; it must not loosen a single budget or turn
truncation into anything other than a typed failure.
"""

from __future__ import annotations

import json

from services.registry.app.society.config import SocietySettings


def test_the_default_can_hold_a_real_docs_candidate(monkeypatch):
    monkeypatch.delenv("SOCIETY_MODEL_MAX_OUTPUT_TOKENS", raising=False)
    from services.registry.app.society.config import reset_settings_cache

    reset_settings_cache()
    s = SocietySettings()
    assert s.model_max_output_tokens >= 4000, "1200 truncated a live Builder into a DEAD run"

    # a realistic docs candidate: four required sections with prose, JSON-escaped
    from services.registry.app.society.engineering.docs_contract import DOCS_REQUIRED_SECTIONS

    body = "# A candidate title\n\n" + "\n\n".join(f"{h}\n\n" + ("prose. " * 60) for h in DOCS_REQUIRED_SECTIONS)
    payload = json.dumps({"decision_summary": "x" * 200, "intents": [{"type": "SUBMIT_CODE_CANDIDATE", "payload": {"candidate_id": "u" * 36, "summary": "s" * 300, "edits": [{"path": "docs/society/candidates/a.md", "content": body}]}}]})
    # ~4 chars/token is the usual rule of thumb for this kind of text
    assert len(payload) / 4 < s.model_max_output_tokens, f"{len(payload)} chars would not fit"


def test_raising_capacity_did_not_raise_any_budget(monkeypatch):
    monkeypatch.delenv("SOCIETY_MODEL_MAX_OUTPUT_TOKENS", raising=False)
    from services.registry.app.society.config import reset_settings_cache

    reset_settings_cache()
    s = SocietySettings()
    # The values this repository declares, asserted so that a future change to
    # capacity cannot quietly ride along with a change to a budget.
    assert str(s.daily_model_budget_usd) == "2.0", "the money cap is unchanged"
    assert s.max_runs_per_correlation == 40
    assert s.max_runs_per_hour == 120
    assert s.max_intents_per_run == 5


def test_truncation_is_still_a_typed_failure():
    """Capacity does not mean an unbounded response, and a cut-off answer must
    still fail loudly rather than be silently half-parsed."""
    import inspect

    from services.registry.app.society import cognition

    src = inspect.getsource(cognition)
    assert "finish_reason=length" in src
    assert "max_tokens" in src and "int(max_tokens)" in src, "the ceiling is always sent"
