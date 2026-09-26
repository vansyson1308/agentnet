"""Invalid model output: strict parsing, a diagnosable error, and an attempt
that is still accounted for.

A live Scout run on a company cycle was dead-lettered after three answers
that were not valid JSON (``Expecting ',' delimiter`` at char 1736). The
run showed no provider and no cost, although the model was called three
times. These tests pin what the model path now guarantees, with the fake
transport only (never a live model):

* parsing stays STRICT -- nothing is repaired or partially accepted;
* the error names the parse position and the SHAPE of the text around it
  (letters/digits masked), never the text itself;
* the error carries the call's accounting (provider, model, format,
  finish_reason, tokens, cost, requests) so the worker can record and bill it.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest

from services.registry.app.society.cognition import (
    FakeModel,
    ModelOutputInvalid,
    OpenAICompatibleModel,
    json_error_detail,
    output_shape,
)
from services.registry.app.society.config import SocietySettings, reset_settings_cache
from services.registry.app.society.context import AgentContext
from services.registry.app.society.intents import DecisionValidationError

SECRET = "sk-test-output-invalid-000000000000"


def _settings(monkeypatch, **env):
    monkeypatch.setenv("SOCIETY_MODEL_PROVIDER", "openai_compatible")
    monkeypatch.setenv("SOCIETY_MODEL_BASE_URL", "https://llm.invalid/v1")
    monkeypatch.setenv("SOCIETY_MODEL_API_KEY", SECRET)
    monkeypatch.setenv("SOCIETY_MODEL_NAME", "test-model")
    monkeypatch.setenv("SOCIETY_MODEL_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("SOCIETY_MODEL_RETRY_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("SOCIETY_MODEL_OUTPUT_FORMAT", "json_object")
    monkeypatch.setenv("SOCIETY_MODEL_USD_PER_1K_INPUT", "0.01")
    monkeypatch.setenv("SOCIETY_MODEL_USD_PER_1K_OUTPUT", "0.03")
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    reset_settings_cache()
    return SocietySettings()


def _context() -> AgentContext:
    return AgentContext(
        prompt_version="t",
        generated_at="now",
        agent={"id": "a", "name": "Society_Scout", "description": ""},
        role="scout",
        mission="observe",
        event={"id": "e", "type": "company.cycle", "correlation_id": "c", "payload": {"_untrusted": True, "source": "event", "data": {"cycle_id": "x"}}},
        goals=[],
        memory=[],
        messages=[],
        proposals=[],
        candidates=[],
        tasks=[],
        budget={},
        permissions={"allowed_intents": ["WRITE_MEMORY", "SLEEP"], "max_intents_per_run": 3},
        restrictions=[],
        recent_activity=[],
    )


# The live failure's pattern: a long text field with an UNESCAPED quote.
MALFORMED = (
    '{"decision_summary": "Evidence reviewed", "intents": [{"type": "WRITE_MEMORY", "payload": '
    '{"title": "cycle", "content": "Outcome was "no_high_value_change" per confidentialcodeword"}}]}'
)


def _answer(content: str, *, finish_reason: str = "stop", prompt_tokens: int = 1000, completion_tokens: int = 200):
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def test_malformed_json_names_the_position_and_shape_never_the_text(monkeypatch):
    settings = _settings(monkeypatch)

    async def transport(payload):
        return _answer(MALFORMED)

    with pytest.raises(ModelOutputInvalid) as err:
        asyncio.run(OpenAICompatibleModel(settings, transport=transport).decide(_context()))
    msg = str(err.value)
    expected_pos = MALFORMED.index('no_high_value_change"')
    assert f"char {expected_pos} of {len(MALFORMED)}" in msg
    # the shape shows the unescaped quote right before the break ...
    assert '"aaaaaaa aaa "<<HERE>>' in msg
    # ... and the call's format and finish reason, for diagnosis
    assert "format=json_object" in msg and "finish_reason=stop" in msg
    # ... but never the words the model wrote
    for word in ("Outcome", "confidentialcodeword", "no_high_value_change", "Evidence"):
        assert word not in msg
    assert SECRET not in msg


def test_invalid_output_carries_the_call_accounting(monkeypatch):
    settings = _settings(monkeypatch)

    async def transport(payload):
        return _answer(MALFORMED, prompt_tokens=1000, completion_tokens=200)

    with pytest.raises(ModelOutputInvalid) as err:
        asyncio.run(OpenAICompatibleModel(settings, transport=transport).decide(_context()))
    a = err.value.attempt
    assert a.provider == "openai_compatible" and a.model_name == "test-model"
    assert a.tokens_in == 1000 and a.tokens_out == 200 and a.requests == 1
    # 1000 * 0.01/1K + 200 * 0.03/1K
    assert a.cost_usd == Decimal("0.016000")
    assert a.output_format == "json_object" and a.finish_reason == "stop"
    # still the strict validation error the worker already handles
    assert isinstance(err.value, DecisionValidationError)


def test_truncation_keeps_its_own_message_and_is_accounted(monkeypatch):
    settings = _settings(monkeypatch, SOCIETY_MODEL_MAX_OUTPUT_TOKENS="100")

    async def transport(payload):
        return _answer(MALFORMED[:80], finish_reason="length")

    with pytest.raises(ModelOutputInvalid, match=r"truncated at max_tokens=100 \(finish_reason=length\)") as err:
        asyncio.run(OpenAICompatibleModel(settings, transport=transport).decide(_context()))
    assert err.value.attempt.finish_reason == "length" and err.value.attempt.tokens_in == 1000


@pytest.mark.parametrize(
    "content",
    [
        MALFORMED,
        '{"decision_summary": "ok", "intents": []} trailing prose',
        '{"decision_summary": "ok", "intents": [',
        'Here is my plan: {"decision_summary": "ok", "intents": []}',
    ],
)
def test_parsing_stays_strict_nothing_is_repaired(monkeypatch, content):
    settings = _settings(monkeypatch)

    async def transport(payload):
        return _answer(content)

    with pytest.raises(ModelOutputInvalid):
        asyncio.run(OpenAICompatibleModel(settings, transport=transport).decide(_context()))


def test_fake_model_reports_invalid_output_the_same_way():
    with pytest.raises(ModelOutputInvalid) as err:
        asyncio.run(FakeModel(['{"decision_summary": "a "b" c"}']).decide(_context()))
    assert err.value.attempt.provider == "fake" and err.value.attempt.output_format == "fake"
    assert "<<HERE>>" in str(err.value)


def test_shape_masks_letters_and_digits_and_keeps_json_structure():
    assert output_shape('{"k": "v 42", "x": [1]}') == '{"a": "a 99", "a": [9]}'
    assert output_shape("é?\t") == "a. "
    try:
        json.loads(MALFORMED)
    except json.JSONDecodeError as exc:
        detail = json_error_detail(MALFORMED, exc, window=10)
    assert detail.startswith(f"char {MALFORMED.index('no_high_value_change')} of {len(MALFORMED)}; shape ")
    assert len(detail) < 80  # bounded
    assert json_error_detail("not json", ValueError("x")) == "8 chars"
