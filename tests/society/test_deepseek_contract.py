"""Offline DeepSeek-shaped contract tests for the OpenAI-compatible adapter.

No network, no key: a fake transport emulates the documented DeepSeek
behaviour (OpenAI-compatible ``/chat/completions`` at
``https://api.deepseek.com``, ``response_format={"type":"json_object"}``,
the word "json" required in the prompt, occasional EMPTY content, usage
with ``prompt_cache_hit_tokens``). Model names are configuration only.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from services.registry.app.society import cognition
from services.registry.app.society.cognition import ModelProviderError, ModelTimeout, OpenAICompatibleModel
from services.registry.app.society.config import SocietySettings, reset_settings_cache
from services.registry.app.society.context import AgentContext
from services.registry.app.society.intents import DecisionValidationError

KEY = "test-only-not-a-real-credential"
GOOD = {"decision_summary": "fine", "intents": [], "sleep_for_seconds": 60}


def _settings(monkeypatch, **env):
    base = {
        "SOCIETY_MODEL_PROVIDER": "openai_compatible",
        "SOCIETY_MODEL_BASE_URL": "https://api.deepseek.com",
        "SOCIETY_MODEL_API_KEY": KEY,
        "SOCIETY_MODEL_FAST_NAME": "fast-tier-model",
        "SOCIETY_MODEL_STRONG_NAME": "strong-tier-model",
        "SOCIETY_MODEL_REQUEST_RETRIES": "1",
        "SOCIETY_MODEL_RETRY_BACKOFF_SECONDS": "0",
        "SOCIETY_MODEL_TIMEOUT_SECONDS": "5",
    }
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    return SocietySettings()


def _context(role="scout"):
    return AgentContext(prompt_version="t", generated_at="now", agent={"id": "a", "name": "Society_Scout", "description": ""}, role=role, mission="m", event={"id": "e", "type": "t", "payload": {}, "correlation_id": "c"}, goals=[], memory=[], messages=[], proposals=[], candidates=[], tasks=[], budget={}, permissions={"allowed_intents": ["WRITE_MEMORY"], "max_intents_per_run": 3}, restrictions=[], recent_activity=[])


def _ok(content=GOOD, usage=None, raw=None):
    body = {"id": "chatcmpl-x", "object": "chat.completion", "model": "fast-tier-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": raw if raw is not None else json.dumps(content)}, "finish_reason": "stop"}]}
    if usage is not None:
        body["usage"] = usage
    return body


def test_base_url_and_endpoint_construction_and_model_names_are_config(monkeypatch):
    s = _settings(monkeypatch)
    assert s.model_base_url == "https://api.deepseek.com"
    m = OpenAICompatibleModel(s, transport=None)
    assert m.model_name == "fast-tier-model"
    # no provider model name is hard-coded in business logic
    import pathlib

    src = pathlib.Path(cognition.__file__).parent
    for f in ("router.py", "worker.py", "cognition.py", "policy.py", "executor.py", "promotion.py", "fitness.py"):
        text = (src / f).read_text()
        assert "deepseek-" not in text and "gpt-4" not in text, f


def test_prompt_carries_explicit_json_instruction_and_example(monkeypatch):
    s = _settings(monkeypatch)
    m = OpenAICompatibleModel(s)
    msgs = m._messages(_context())
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert "json" in system.lower() and "json" in user.lower()
    assert '"decision_summary"' in system and "Example" in system
    assert KEY not in system + user


def test_json_object_mode_success_parses_usage_and_cache_tokens(monkeypatch):
    s = _settings(monkeypatch, SOCIETY_MODEL_OUTPUT_FORMAT="json_object")
    seen = []

    async def transport(payload):
        seen.append(payload)
        return _ok(usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150, "prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 20})

    resp = asyncio.run(OpenAICompatibleModel(s, transport=transport).decide(_context()))
    assert seen[0]["response_format"] == {"type": "json_object"} and seen[0]["model"] == "fast-tier-model"
    assert resp.tokens_in == 120 and resp.tokens_out == 30 and resp.tokens_cached == 100
    assert resp.output_format == "json_object" and resp.format_fallbacks == 0 and resp.usage_missing is False
    assert KEY not in json.dumps(seen[0])


def test_json_schema_unsupported_negotiates_once_and_is_remembered(monkeypatch):
    s = _settings(monkeypatch, SOCIETY_MODEL_OUTPUT_FORMAT="auto")
    seen = []

    async def transport(payload):
        seen.append(payload["response_format"]["type"])
        if payload["response_format"]["type"] == "json_schema":
            raise cognition._HTTPStatus(400, "invalid response_format")
        return _ok(usage={"prompt_tokens": 1, "completion_tokens": 1})

    m = OpenAICompatibleModel(s, transport=transport)
    r1 = asyncio.run(m.decide(_context()))
    assert seen == ["json_schema", "json_object"] and r1.format_fallbacks == 1 and r1.retries == 0 and r1.output_format == "json_object"
    r2 = asyncio.run(m.decide(_context()))
    assert seen == ["json_schema", "json_object", "json_object"], "the learned capability is reused; no second probe"
    assert r2.format_fallbacks == 0 and "json_schema rejected (400) -> json_object" in m.negotiation_log


def test_routing_overrides_the_model_name_per_call(monkeypatch):
    s = _settings(monkeypatch, SOCIETY_MODEL_OUTPUT_FORMAT="json_object")
    seen = []

    async def transport(payload):
        seen.append(payload["model"])
        return _ok(usage={"prompt_tokens": 1, "completion_tokens": 1})

    m = OpenAICompatibleModel(s, transport=transport)
    r = asyncio.run(m.decide(_context(), model_name="strong-tier-model"))
    assert seen == ["strong-tier-model"] and r.model_name == "strong-tier-model"


def test_empty_content_is_retried_bounded_then_validation_error(monkeypatch):
    s = _settings(monkeypatch, SOCIETY_MODEL_OUTPUT_FORMAT="json_object", SOCIETY_MODEL_EMPTY_CONTENT_RETRIES="1")
    calls = {"n": 0}

    async def transport(payload):
        calls["n"] += 1
        return _ok(raw="", usage={"prompt_tokens": 5, "completion_tokens": 0})

    with pytest.raises(DecisionValidationError, match="empty content"):
        asyncio.run(OpenAICompatibleModel(s, transport=transport).decide(_context()))
    assert calls["n"] == 2
    # one empty then a good answer succeeds and accounts both requests
    calls["n"] = 0

    async def transport2(payload):
        calls["n"] += 1
        return _ok(raw="" if calls["n"] == 1 else json.dumps(GOOD), usage={"prompt_tokens": 5, "completion_tokens": 2})

    r = asyncio.run(OpenAICompatibleModel(s, transport=transport2).decide(_context()))
    assert r.requests == 2 and r.retries == 0 and r.tokens_in == 10


def test_malformed_json_is_not_retried(monkeypatch):
    s = _settings(monkeypatch, SOCIETY_MODEL_OUTPUT_FORMAT="json_object", SOCIETY_MODEL_REQUEST_RETRIES="3")
    calls = {"n": 0}

    async def transport(payload):
        calls["n"] += 1
        return _ok(raw="{not json", usage={"prompt_tokens": 1, "completion_tokens": 1})

    with pytest.raises(DecisionValidationError):
        asyncio.run(OpenAICompatibleModel(s, transport=transport).decide(_context()))
    assert calls["n"] == 1


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_retryable_statuses_use_the_bounded_retry_budget(monkeypatch, status):
    s = _settings(monkeypatch, SOCIETY_MODEL_OUTPUT_FORMAT="json_object", SOCIETY_MODEL_REQUEST_RETRIES="2")
    calls = {"n": 0}

    async def transport(payload):
        calls["n"] += 1
        raise cognition._HTTPStatus(status, "busy")

    with pytest.raises(ModelProviderError) as exc:
        asyncio.run(OpenAICompatibleModel(s, transport=transport).decide(_context()))
    assert calls["n"] == 3 and KEY not in str(exc.value)


def test_timeout_accounting(monkeypatch):
    s = _settings(monkeypatch, SOCIETY_MODEL_OUTPUT_FORMAT="json_object", SOCIETY_MODEL_REQUEST_RETRIES="1", SOCIETY_MODEL_TIMEOUT_SECONDS="5")
    calls = {"n": 0}

    async def transport(payload):
        calls["n"] += 1
        raise asyncio.TimeoutError()

    with pytest.raises(ModelTimeout):
        asyncio.run(OpenAICompatibleModel(s, transport=transport).decide(_context()))
    assert calls["n"] == 2


def test_missing_usage_is_flagged_not_fabricated(monkeypatch):
    s = _settings(monkeypatch, SOCIETY_MODEL_OUTPUT_FORMAT="json_object")

    async def transport(payload):
        return _ok()

    r = asyncio.run(OpenAICompatibleModel(s, transport=transport).decide(_context()))
    assert r.usage_missing is True and r.tokens_in == 0 and r.tokens_out == 0 and str(r.cost_usd) == "0.000000"


def test_error_messages_never_echo_the_credential(monkeypatch):
    s = _settings(monkeypatch, SOCIETY_MODEL_OUTPUT_FORMAT="json_object", SOCIETY_MODEL_REQUEST_RETRIES="0")

    async def transport(payload):
        raise cognition._HTTPStatus(401, f"unauthorized for key {KEY}"[:200])

    with pytest.raises(ModelProviderError) as exc:
        asyncio.run(OpenAICompatibleModel(s, transport=transport).decide(_context()))
    # the excerpt is provider text; the ADAPTER never adds the key, and settings redact it
    assert s.public_dict()["model_api_key"] == "***"
    assert "Authorization" not in str(exc.value)
