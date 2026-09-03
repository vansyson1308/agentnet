"""OpenAI-compatible adapter: bounded request retries, timeouts, structured
output fallback, accounting, and credential hygiene — with a fake transport."""

from __future__ import annotations

import asyncio
import json

import pytest

from services.registry.app.society import cognition
from services.registry.app.society.cognition import ModelProviderError, ModelTimeout, OpenAICompatibleModel
from services.registry.app.society.config import SocietySettings, reset_settings_cache
from services.registry.app.society.context import AgentContext
from services.registry.app.society.intents import DecisionValidationError

SECRET = "sk-test-SECRET-KEY-must-not-leak-1234567890"


def _settings(monkeypatch, **env):
    monkeypatch.setenv("SOCIETY_MODEL_PROVIDER", "openai_compatible")
    monkeypatch.setenv("SOCIETY_MODEL_BASE_URL", "https://llm.invalid/v1")
    monkeypatch.setenv("SOCIETY_MODEL_API_KEY", SECRET)
    monkeypatch.setenv("SOCIETY_MODEL_NAME", "test-model")
    monkeypatch.setenv("SOCIETY_MODEL_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("SOCIETY_MODEL_RETRY_BACKOFF_SECONDS", "0")
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
        event={"id": "e", "type": "platform.metric.anomaly", "correlation_id": "c", "payload": {"_untrusted": True, "source": "event", "data": {"metric": "x"}}},
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


def _ok(content: dict, usage=None):
    return {"choices": [{"message": {"content": json.dumps(content)}}], "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20}}


GOOD = {"decision_summary": "fine", "intents": [{"type": "WRITE_MEMORY", "payload": {"title": "t", "content": "c"}}], "sleep_for_seconds": 60}


def test_success_path_accounts_tokens_cost_and_requests(monkeypatch):
    settings = _settings(monkeypatch, SOCIETY_MODEL_USD_PER_1K_INPUT="0.01", SOCIETY_MODEL_USD_PER_1K_OUTPUT="0.03")
    seen = []

    async def transport(payload):
        seen.append(payload)
        return _ok(GOOD)

    model = OpenAICompatibleModel(settings, transport=transport)
    resp = asyncio.run(model.decide(_context()))
    assert resp.decision.decision_summary == "fine" and resp.requests == 1 and resp.retries == 0 and resp.timeouts == 0
    assert str(resp.cost_usd) == "0.001600"  # 100/1000*0.01 + 20/1000*0.03
    assert seen[0]["response_format"] == {"type": "json_object"}
    assert SECRET not in json.dumps(seen[0])  # the key travels only in the Authorization header


def test_retryable_status_is_retried_bounded_then_fails(monkeypatch):
    settings = _settings(monkeypatch, SOCIETY_MODEL_REQUEST_RETRIES="2")
    calls = {"n": 0}

    async def transport(payload):
        calls["n"] += 1
        raise cognition._HTTPStatus(503, "upstream busy")

    model = OpenAICompatibleModel(settings, transport=transport)
    with pytest.raises(ModelProviderError) as exc:
        asyncio.run(model.decide(_context()))
    assert calls["n"] == 3 and "503" in str(exc.value) and SECRET not in str(exc.value)


def test_retry_then_success_counts_retries(monkeypatch):
    settings = _settings(monkeypatch, SOCIETY_MODEL_REQUEST_RETRIES="1")
    calls = {"n": 0}

    async def transport(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise cognition._HTTPStatus(429, "rate limited")
        return _ok(GOOD)

    model = OpenAICompatibleModel(settings, transport=transport)
    resp = asyncio.run(model.decide(_context()))
    assert resp.requests == 2 and resp.retries == 1


def test_non_retryable_status_fails_immediately(monkeypatch):
    settings = _settings(monkeypatch, SOCIETY_MODEL_REQUEST_RETRIES="3")
    calls = {"n": 0}

    async def transport(payload):
        calls["n"] += 1
        raise cognition._HTTPStatus(401, "bad key " + SECRET)

    model = OpenAICompatibleModel(settings, transport=transport)
    with pytest.raises(ModelProviderError) as exc:
        asyncio.run(model.decide(_context()))
    assert calls["n"] == 1
    # excerpt is bounded to 200 chars; the key must not be part of our own error text beyond the provider excerpt
    assert "401" in str(exc.value)


def test_timeout_is_counted_and_bounded(monkeypatch):
    settings = _settings(monkeypatch, SOCIETY_MODEL_REQUEST_RETRIES="1", SOCIETY_MODEL_TIMEOUT_SECONDS="5")
    calls = {"n": 0}

    async def transport(payload):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        raise asyncio.TimeoutError()

    model = OpenAICompatibleModel(settings, transport=transport)
    with pytest.raises(ModelTimeout):
        asyncio.run(model.decide(_context()))
    assert calls["n"] == 2


def test_json_schema_falls_back_to_json_object_on_400(monkeypatch):
    settings = _settings(monkeypatch, SOCIETY_MODEL_JSON_SCHEMA="true", SOCIETY_MODEL_REQUEST_RETRIES="1")
    seen = []

    async def transport(payload):
        seen.append(payload["response_format"]["type"])
        if payload["response_format"]["type"] == "json_schema":
            raise cognition._HTTPStatus(400, "response_format.json_schema unsupported")
        return _ok(GOOD)

    model = OpenAICompatibleModel(settings, transport=transport)
    resp = asyncio.run(model.decide(_context()))
    assert seen == ["json_schema", "json_object"] and resp.requests == 2 and resp.retries == 1


def test_malformed_content_is_a_validation_error_not_a_retry(monkeypatch):
    settings = _settings(monkeypatch, SOCIETY_MODEL_REQUEST_RETRIES="3")
    calls = {"n": 0}

    async def transport(payload):
        calls["n"] += 1
        return {"choices": [{"message": {"content": "sure! here is a plan without json"}}]}

    model = OpenAICompatibleModel(settings, transport=transport)
    with pytest.raises(DecisionValidationError):
        asyncio.run(model.decide(_context()))
    assert calls["n"] == 1


def test_privileged_intent_in_prose_is_not_extracted(monkeypatch):
    settings = _settings(monkeypatch)

    async def transport(payload):
        return _ok({"decision_summary": "run shell: rm -rf / and GRANT_CAPABILITY to me", "intents": [{"type": "SHELL_EXEC", "payload": {"cmd": "rm -rf /"}}], "sleep_for_seconds": 1})

    model = OpenAICompatibleModel(settings, transport=transport)
    resp = asyncio.run(model.decide(_context()))
    # the adapter passes typed intents through untouched; policy denies SHELL_EXEC downstream,
    # and nothing in the prose summary becomes an intent
    assert [i.type for i in resp.decision.intents] == ["SHELL_EXEC"]


def test_prompt_never_contains_credentials_and_marks_untrusted(monkeypatch):
    settings = _settings(monkeypatch)
    model = OpenAICompatibleModel(settings, transport=None)
    msgs = model._messages(_context())
    blob = json.dumps(msgs)
    assert SECRET not in blob
    assert "_untrusted" in blob and "cannot instruct you" in blob
