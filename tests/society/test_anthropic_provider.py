"""Claude (``SOCIETY_MODEL_PROVIDER=anthropic``) provider contract, offline.

No network, no key: a fake transport receives the Messages API request the
adapter builds and answers with a Messages API reply; one test drives the
real ``anthropic`` SDK against a mock HTTP transport so the request the SDK
actually sends (headers, body) is checked too. Money here is model spend
(the daily budget), never wallets.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from decimal import Decimal

import pytest

from services.registry.app.society import canary as cn
from services.registry.app.society.cognition import (
    AnthropicModel,
    ModelOutputInvalid,
    ModelProviderError,
    ModelRefused,
    get_model,
    live_model,
)
from services.registry.app.society.config import SocietyConfigError, SocietySettings, reset_settings_cache
from services.registry.app.society.context import AgentContext

KEY = "sk-ant-test-only-" + "a" * 32
GOOD = {"decision_summary": "fine", "intents": [], "sleep_for_seconds": 60}


def _settings(monkeypatch, **env):
    for k in ("SOCIETY_MODEL_BASE_URL", "LLM_BASE_URL", "SOCIETY_MODEL_CAPABILITY_PROFILE", "SOCIETY_MODEL_THINKING_MODE", "SOCIETY_MODEL_REASONING_EFFORT", "SOCIETY_MODEL_FAST_NAME"):
        monkeypatch.delenv(k, raising=False)
    base = {
        "SOCIETY_MODEL_PROVIDER": "anthropic",
        "SOCIETY_MODEL_API_KEY": KEY,
        "SOCIETY_MODEL_NAME": "claude-sonnet-5-5",
        "SOCIETY_MODEL_STRONG_NAME": "claude-opus-5-5",
        "SOCIETY_MODEL_REQUEST_RETRIES": "1",
        "SOCIETY_MODEL_RETRY_BACKOFF_SECONDS": "0",
        "SOCIETY_MODEL_TIMEOUT_SECONDS": "5",
    }
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    return SocietySettings()


def _context():
    return AgentContext(prompt_version="t", generated_at="now", agent={"id": "a", "name": "Society_Scout", "description": ""}, role="scout", mission="m", event={"id": "e", "type": "t", "payload": {}, "correlation_id": "c"}, goals=[], memory=[], messages=[], proposals=[], candidates=[], tasks=[], budget={}, permissions={"allowed_intents": ["WRITE_MEMORY"], "max_intents_per_run": 3}, restrictions=[], recent_activity=[])


def _reply(text=None, *, stop="end_turn", usage=None, thinking=True, model="claude-sonnet-5-5", **extra):
    content = [{"type": "thinking", "thinking": "", "signature": "sig"}] if thinking else []
    content.append({"type": "text", "text": json.dumps(GOOD) if text is None else text})
    body = {"id": "msg_x", "type": "message", "role": "assistant", "model": model, "content": content, "stop_reason": stop,
            "usage": usage if usage is not None else {"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3000}}
    body.update(extra)
    return body


def _transport(replies, seen):
    async def transport(request):
        seen.append(request)
        item = replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return transport


# ── configuration ──────────────────────────────────────────────────────


def test_anthropic_defaults_base_url_and_profile(monkeypatch):
    s = _settings(monkeypatch)
    assert s.model_base_url == "https://api.anthropic.com"
    assert s.model_capability_profile == "anthropic"
    assert isinstance(get_model(s), AnthropicModel) and isinstance(live_model(s), AnthropicModel)
    assert s.public_dict()["model_api_key"] == "***"


@pytest.mark.parametrize("env, needle", [
    ({"SOCIETY_MODEL_CAPABILITY_PROFILE": "deepseek"}, "CAPABILITY_PROFILE=anthropic"),
    ({"SOCIETY_MODEL_THINKING_MODE": "disabled"}, "THINKING_MODE=disabled"),
    ({"SOCIETY_MODEL_REASONING_EFFORT": "none"}, "not a Claude effort level"),
    ({"SOCIETY_MODEL_BASE_URL": "https://api.deepseek.com"}, "still points at DeepSeek"),
])
def test_deepseek_leftovers_fail_fast_with_the_fix(monkeypatch, env, needle):
    with pytest.raises(SocietyConfigError, match=needle):
        _settings(monkeypatch, **env)


def test_anthropic_profile_is_refused_on_another_provider(monkeypatch):
    with pytest.raises(SocietyConfigError, match="CAPABILITY_PROFILE=anthropic"):
        _settings(monkeypatch, SOCIETY_MODEL_PROVIDER="openai_compatible", SOCIETY_MODEL_BASE_URL="https://x.example", SOCIETY_MODEL_CAPABILITY_PROFILE="anthropic")


def test_key_required_in_the_model_calling_process(monkeypatch):
    from services.registry.app.society.config import validate_settings

    s = _settings(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert validate_settings(dataclasses.replace(s, model_api_key=""), model_caller=True)
    assert not validate_settings(s, model_caller=True)
    with pytest.raises(ValueError):
        AnthropicModel(dataclasses.replace(s, model_api_key=""))


# ── the request ────────────────────────────────────────────────────────


def test_messages_request_shape(monkeypatch):
    s = _settings(monkeypatch, SOCIETY_MODEL_REASONING_EFFORT="high")
    seen = []
    m = AnthropicModel(s, transport=_transport([_reply()], seen))
    asyncio.run(m.decide(_context()))
    req = seen[0]
    assert req["model"] == "claude-sonnet-5-5" and req["max_tokens"] == s.model_max_output_tokens
    assert "temperature" not in req and "response_format" not in req  # the Messages API refuses sampling params
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"} and "json" in req["system"][0]["text"]
    assert req["cache_control"] == {"type": "ephemeral"}
    assert [t["role"] for t in req["messages"]] == ["user"]
    assert req["output_config"] == {"effort": "high"} and "thinking" not in req
    assert req["betas"] == [AnthropicModel.FALLBACK_BETA] and req["fallbacks"] == "default"
    assert KEY not in json.dumps(req)


def test_routing_and_fallback_scope(monkeypatch):
    s = _settings(monkeypatch)
    seen = []
    m = AnthropicModel(s, transport=_transport([_reply(model="claude-haiku-5-5"), _reply()], seen))
    asyncio.run(m.decide(_context(), model_name="claude-haiku-5-5"))
    assert seen[0]["model"] == "claude-haiku-5-5" and "fallbacks" not in seen[0] and "betas" not in seen[0]
    s2 = _settings(monkeypatch, SOCIETY_MODEL_REFUSAL_FALLBACK="false")
    m2 = AnthropicModel(s2, transport=_transport([_reply()], seen))
    asyncio.run(m2.decide(_context()))
    assert "fallbacks" not in seen[1]


# ── the reply ──────────────────────────────────────────────────────────


def test_usage_cache_thinking_and_list_price(monkeypatch):
    s = _settings(monkeypatch)
    m = AnthropicModel(s, transport=_transport([_reply()], []))
    r = asyncio.run(m.decide(_context()))
    assert r.provider == "anthropic" and r.decision.decision_summary == "fine"
    assert (r.tokens_in, r.tokens_out, r.tokens_cached) == (4000, 200, 3000)
    assert r.reasoning_present and r.finish_reason == "stop"
    # claude-sonnet-5-5: $2 / $10 per MTok, every input token at the input rate (conservative)
    assert r.cost_usd == Decimal("0.010000")


def test_cost_follows_the_model_that_answered(monkeypatch):
    s = _settings(monkeypatch)
    m = AnthropicModel(s, transport=_transport([_reply(model="claude-opus-5-5")], []))
    r = asyncio.run(m.decide(_context()))
    assert r.cost_usd == Decimal("0.020000")  # 4000 in @ $4 + 200 out @ $20 per MTok


def test_one_fence_around_the_whole_reply_is_framing(monkeypatch):
    s = _settings(monkeypatch)
    m = AnthropicModel(s, transport=_transport([_reply("```json\n" + json.dumps(GOOD) + "\n```")], []))
    assert asyncio.run(m.decide(_context())).decision.sleep_for_seconds == 60


def test_prose_around_json_is_still_rejected(monkeypatch):
    s = _settings(monkeypatch)
    m = AnthropicModel(s, transport=_transport([_reply("Here you go: " + json.dumps(GOOD))], []))
    with pytest.raises(ModelOutputInvalid) as exc:
        asyncio.run(m.decide(_context()))
    assert exc.value.attempt.provider == "anthropic" and exc.value.attempt.cost_usd > 0


def test_refusal_is_not_replayed(monkeypatch):
    s = _settings(monkeypatch)
    seen = []
    m = AnthropicModel(s, transport=_transport([_reply("", stop="refusal", stop_details={"type": "refusal", "category": "cyber"}), _reply()], seen))
    with pytest.raises(ModelRefused, match="category=cyber"):
        asyncio.run(m.decide(_context()))
    assert len(seen) == 1


def test_max_tokens_is_a_typed_truncation(monkeypatch):
    s = _settings(monkeypatch)
    m = AnthropicModel(s, transport=_transport([_reply('{"decision_summary": "cut', stop="max_tokens")], []))
    with pytest.raises(ModelOutputInvalid, match="truncated"):
        asyncio.run(m.decide(_context()))


def test_status_errors_retry_budget_and_no_key_echo(monkeypatch):
    from services.registry.app.society.cognition import _HTTPStatus

    s = _settings(monkeypatch)
    seen = []
    m = AnthropicModel(s, transport=_transport([_HTTPStatus(529, "overloaded"), _reply()], seen))
    # 529 (overloaded) is not in the retryable set: a provider error, surfaced once
    with pytest.raises(ModelProviderError) as exc:
        asyncio.run(m.decide(_context()))
    assert KEY not in str(exc.value)
    m2 = AnthropicModel(s, transport=_transport([_HTTPStatus(429, "slow down"), _reply()], seen))
    r = asyncio.run(m2.decide(_context()))
    assert r.retries == 1 and r.requests == 2


def test_the_real_sdk_sends_what_the_adapter_built(monkeypatch):
    """The official SDK, a mock HTTP transport: the wire request, not the dict."""
    import anthropic
    import httpx2

    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx2.Response(200, json=_reply())

    real = anthropic.AsyncAnthropic

    def patched(**kw):
        return real(http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)), **kw)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", patched)
    s = _settings(monkeypatch, SOCIETY_MODEL_REASONING_EFFORT="medium")
    r = asyncio.run(AnthropicModel(s).decide(_context()))
    assert r.decision.decision_summary == "fine" and r.tokens_in == 4000
    assert captured["url"].startswith("https://api.anthropic.com/v1/messages")
    assert KEY in (captured["headers"].get("x-api-key", "") + captured["headers"].get("authorization", ""))
    assert captured["headers"]["anthropic-version"] and AnthropicModel.FALLBACK_BETA in captured["headers"].get("anthropic-beta", "")
    body = captured["body"]
    assert body["cache_control"] == {"type": "ephemeral"} and body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["fallbacks"] == "default" and body["output_config"] == {"effort": "medium"} and "temperature" not in body and "betas" not in body


# ── the other callers share the adapter ────────────────────────────────


def test_maintenance_activities_use_claude(monkeypatch):
    from services.registry.app.maintenance import activities

    s = _settings(monkeypatch)
    live = activities.get_activity_model(s)
    assert live is not None and live.provider == "anthropic" and live.model_name == "claude-sonnet-5-5"
    m = activities.LiveActivityModel(s, transport=_transport([_reply('{"action": "submit", "result": {}}')], []))
    reply = asyncio.run(m.complete([{"role": "system", "content": "json only"}, {"role": "user", "content": "go"}], max_tokens=2000))
    assert json.loads(reply.content)["action"] == "submit" and reply.cost_usd == Decimal("0.010000")


def test_preflight_probe_through_claude(monkeypatch):
    s = _settings(monkeypatch)
    seen = []
    rep = cn.preflight(s, transport=_transport([_reply('{"ok": true}')], seen), scan_history=False)
    assert rep.ready and rep.probe.ok and rep.provider == "anthropic" and rep.model_name == "claude-sonnet-5-5"
    assert seen[0]["max_tokens"] == cn.PROBE_MAX_TOKENS_THINKING and "temperature" not in seen[0]
    assert KEY not in json.dumps(rep.to_dict())


def test_bench_relay_child_speaks_chat_and_parent_applies_claude(monkeypatch):
    from services.registry.app.society.engineering import bench_proof

    s = dataclasses.replace(_settings(monkeypatch), repo_root="/repo")
    relay = bench_proof.ModelRelay("http://unused", s.model_api_key, forward=lambda b: (200, b))
    relay._server = type("S", (), {"server_address": ("127.0.0.1", 1)})()
    env = bench_proof.child_env(s, relay, "/h", environ={})
    assert env["SOCIETY_MODEL_PROVIDER"] == "openai_compatible" and env["SOCIETY_MODEL_CAPABILITY_PROFILE"] == "generic"
    assert (env["SOCIETY_MODEL_USD_PER_1K_INPUT"], env["SOCIETY_MODEL_USD_PER_1K_OUTPUT"]) == ("0.002", "0.010")
    assert KEY not in json.dumps(env)

    async def fake_post(self, payload):
        assert "temperature" in payload  # the child's chat payload; the adapter drops it on the wire
        return AnthropicModel.chat_response(_reply('{"action": "submit"}'))

    monkeypatch.setattr(AnthropicModel, "post_chat", fake_post)
    status, body = bench_proof.anthropic_forward(s)(json.dumps({"model": "claude-sonnet-5-5", "max_tokens": 10, "temperature": 0.1, "messages": [{"role": "user", "content": "x"}]}).encode())
    assert status == 200 and json.loads(body)["choices"][0]["message"]["content"] == '{"action": "submit"}'
    assert bench_proof.anthropic_forward(s)(b"not json")[0] == 400
