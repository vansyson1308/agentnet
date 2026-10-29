"""Prompt caching for the Claude provider, offline (Anthropic email 2026-10-29).

What stays true:

* the model sees the same information: the decide prompt is the same rules,
  schema doc, identity and context JSON -- only their ORDER changes, most
  stable first, so a byte-identical prefix repeats across runs;
* the static system block (rules + schema doc) is identical for every agent,
  role, allowed set and run, and long enough to cache (>= the 512-token
  minimum of the Claude 5 family);
* the context JSON is one object whose top-level keys are tiered stable ->
  slow -> volatile; cache breakpoints sit at the end of the stable tiers and
  nothing per-run (timestamps, run/event ids, budget) precedes them;
* TTL follows cadence: an agent whose runs are usually > 5 minutes apart gets
  the 1-hour cache on its stable prefix; hot loops stay on 5 minutes;
* cost accounting stays exact: reads, 5-minute writes (1.25x) and 1-hour
  writes (2x) are priced per model from the official price table
  (https://platform.claude.com/docs/en/about-claude/pricing, "Prompt caching").

Money here is model spend (the daily budget), never wallets.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services.registry.app.society import cognition
from services.registry.app.society.cognition import AnthropicModel, ChatOutcome, OpenAICompatibleModel
from services.registry.app.society.config import (
    ANTHROPIC_CACHE_READ_X,
    ANTHROPIC_CACHE_READ_X_DEFAULT,
    ANTHROPIC_CACHE_WRITE_1H_X,
    ANTHROPIC_CACHE_WRITE_X,
    ANTHROPIC_MIN_CACHEABLE_TOKENS,
    ANTHROPIC_USD_PER_1K,
    SocietyConfigError,
)
from services.registry.app.society.context import AgentContext

from .test_anthropic_provider import GOOD, _reply, _settings, _transport

REPO = pathlib.Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def _ctx(*, name="Society_Scout", role="scout", allowed=("WRITE_MEMORY", "SEND_MESSAGE"), run_id="r1", event_id="e1",
         now=NOW, gaps_min=(30, 30, 30), memory=(), budget=None, **extra):
    at, activity = now, []
    for g in gaps_min:
        at = at - timedelta(minutes=g)
        activity.append({"run_id": f"old-{g}-{at.minute}", "event_type": "t", "status": "completed", "decision": "d", "intents": 0,
                         "outcomes": {"executed": 0, "not_executed": []}, "at": at.isoformat()})
    base = dict(prompt_version="p7", generated_at=now.isoformat(), agent={"id": "a-" + name, "name": name, "description": ""}, role=role,
                mission=f"mission of {role}", event={"id": event_id, "type": "society.heartbeat", "payload": {"_untrusted": True, "source": "event:system", "data": {}}, "correlation_id": "c"},
                goals=[{"id": "g", "title": "grow"}], memory=list(memory), messages=[], proposals=[], candidates=[], tasks=[],
                budget=budget or {"daily_usd": 2.5, "spent_usd": 0.31}, permissions={"allowed_intents": list(allowed), "max_intents_per_run": 3},
                restrictions=["You cannot change your own permissions, budget, wallet, or any secret."], recent_activity=activity, run_id=run_id)
    base.update(extra)
    return AgentContext(**base)


def _request(model, ctx):
    return model.messages_request(model.build_chat_request(messages=model._messages(ctx), max_tokens=100, response_format={"type": "json_object"}))


def _cached_prefix(req):
    """The bytes up to (and including) the LAST block that carries a breakpoint."""
    blocks = [("system", b) for b in req.get("system", [])] + [("user", b) for b in req["messages"][0]["content"]]
    last = max(i for i, (_, b) in enumerate(blocks) if "cache_control" in b)
    return "".join(b["text"] for _, b in blocks[: last + 1])


# ── the layout: same information, stable first ─────────────────────────


def test_the_static_system_block_is_identical_for_every_agent_role_and_run(monkeypatch):
    m = AnthropicModel(_settings(monkeypatch))
    a = _request(m, _ctx())
    b = _request(m, _ctx(name="Society_Governor", role="governor", allowed=("DECIDE_DESIGN",), run_id="r2", event_id="e2", now=NOW + timedelta(hours=3)))
    assert a["system"][0]["text"] == b["system"][0]["text"], "one shared cache entry for the whole fleet"
    static = a["system"][0]["text"]
    for per_agent in ("Society_Scout", "mission of scout", "WRITE_MEMORY, SEND_MESSAGE", "r1", "e1", NOW.isoformat()):
        assert per_agent not in static
    assert a["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    # long enough to cache: a conservative 4 characters per token still clears the minimum
    assert len(static) // 4 >= ANTHROPIC_MIN_CACHEABLE_TOKENS * 4
    assert static == cognition.static_system_prompt() and cognition.static_system_prompt() == cognition.static_system_prompt()


def test_the_model_sees_the_same_information_in_one_rendering(monkeypatch):
    ctx = _ctx()
    req = _request(AnthropicModel(_settings(monkeypatch)), ctx)
    system = "".join(b["text"] for b in req["system"])
    user = "".join(b["text"] for b in req["messages"][0]["content"])
    # the identity, mission, allowed intents and limit are all still stated
    assert 'You are Society_Scout, an autonomous agent inside AgentNet with the role "scout"' in system
    assert "Mission: mission of scout" in system and "WRITE_MEMORY, SEND_MESSAGE" in system and "at most 3 intents" in system
    assert "Intent payload schemas (json):" in system and "NOT asked again" in system
    # the context is still ONE json object with exactly the context's fields and values
    assert user.startswith("CONTEXT (json):\n") and user.endswith("\n\nRespond with the json decision object only.")
    body = user[len("CONTEXT (json):\n"): -len("\n\nRespond with the json decision object only.")]
    assert json.loads(body) == json.loads(ctx.canonical_json())
    # a separator between blocks would still be valid json: every cut is after a top-level comma
    blocks = [b["text"] for b in req["messages"][0]["content"]]
    assert all(t.endswith(",") for t in blocks[:-1])
    assert json.loads(" ".join(blocks)[len("CONTEXT (json):\n"): -len("\n\nRespond with the json decision object only.")]) == json.loads(ctx.canonical_json())
    # the OpenAI-compatible path renders the same text as one string
    oa = OpenAICompatibleModel._messages(AnthropicModel(_settings(monkeypatch)), ctx)
    assert oa[0]["content"] == system and oa[1]["content"] == user
    assert all(set(msg) == {"role", "content"} for msg in OpenAICompatibleModel(_settings(monkeypatch, SOCIETY_MODEL_PROVIDER="openai_compatible", SOCIETY_MODEL_BASE_URL="https://x.example", SOCIETY_MODEL_CAPABILITY_PROFILE="generic"))._messages(ctx))


def test_nothing_per_run_precedes_a_breakpoint(monkeypatch):
    m = AnthropicModel(_settings(monkeypatch))
    a = _request(m, _ctx())
    # same agent, next run: new event, run id, timestamps, budget, activity
    b = _request(m, _ctx(run_id="r9", event_id="e9", now=NOW + timedelta(minutes=40), budget={"daily_usd": 2.5, "spent_usd": 0.99}))
    assert _cached_prefix(a) == _cached_prefix(b)
    prefix = _cached_prefix(a)
    for volatile in ("generated_at", "run_id", "event", "budget", "recent_activity", "engineering", "signal_coverage", "repo_reads"):
        assert f'"{volatile}":' not in prefix
    for stable in ("agent", "mission", "permissions", "restrictions", "goals", "society_agents"):
        assert f'"{stable}":' in prefix
    # a slow field (memory) changes only what follows the stable head
    c = _request(m, _ctx(memory=[{"title": "new lesson"}]))
    assert c["system"] == a["system"] and c["messages"][0]["content"][0] == a["messages"][0]["content"][0]
    assert c["messages"][0]["content"][1]["text"] != a["messages"][0]["content"][1]["text"]


def test_a_new_context_field_is_volatile_by_default():
    tiers = cognition.context_tiers(_ctx())
    stable, slow, volatile = (set(json.loads("{" + t.rstrip(",") + "}")) for t in (tiers[0][1:], tiers[1], tiers[2][:-1]))
    assert stable | slow | volatile == set(_ctx().to_dict()) and not (stable & slow or stable & volatile or slow & volatile)
    assert {"generated_at", "run_id", "event", "budget", "recent_activity"} <= volatile
    assert set(cognition.CONTEXT_TIERS["stable"]) == stable and set(cognition.CONTEXT_TIERS["slow"]) == slow


# ── TTL by cadence ─────────────────────────────────────────────────────


def _ttls(req):
    blocks = list(req.get("system", [])) + list(req["messages"][0]["content"])
    return [b["cache_control"].get("ttl", "5m") if "cache_control" in b else None for b in blocks]


def test_ttl_follows_the_agents_cadence(monkeypatch):
    m = AnthropicModel(_settings(monkeypatch))
    # runs ~30 minutes apart: the stable prefix lives an hour; the slow tier is not worth a write
    assert _ttls(_request(m, _ctx(gaps_min=(30, 25, 45)))) == ["1h", None, "1h", None, None]
    # runs ~2 minutes apart (a review round): 5 minutes everywhere, and the slow tier is cached too
    assert _ttls(_request(m, _ctx(gaps_min=(2, 1, 3)))) == ["1h", None, "5m", "5m", None]
    # no history yet: the brief's cadence (minutes to an hour apart)
    assert _ttls(_request(m, _ctx(gaps_min=())))[2] == "1h"
    assert cognition.cadence_seconds(_ctx(gaps_min=(30, 25, 45))) == 30 * 60
    assert cognition.cadence_seconds(_ctx(gaps_min=())) is None


def test_ttl_setting_and_limits(monkeypatch):
    five = AnthropicModel(_settings(monkeypatch, SOCIETY_MODEL_CACHE_TTL="5m"))
    assert _ttls(_request(five, _ctx())) == ["5m", None, "5m", None, None]
    one = AnthropicModel(_settings(monkeypatch, SOCIETY_MODEL_CACHE_TTL="1h"))
    assert _ttls(_request(one, _ctx(gaps_min=(2, 1, 3)))) == ["1h", None, "1h", "5m", None]
    off = _request(AnthropicModel(_settings(monkeypatch, SOCIETY_MODEL_CACHE_TTL="off")), _ctx())
    assert "cache_control" not in json.dumps(off)
    with pytest.raises(SocietyConfigError, match="SOCIETY_MODEL_CACHE_TTL"):
        _settings(monkeypatch, SOCIETY_MODEL_CACHE_TTL="2h")
    for req in (_request(five, _ctx(gaps_min=(1, 1))), _request(one, _ctx(gaps_min=(1, 1)))):
        marks = _ttls(req)
        assert sum(t is not None for t in marks) <= 4, "the API allows 4 breakpoints"
        seen = [t for t in marks if t]
        assert seen == sorted(seen, key=lambda t: t != "1h"), "a 1-hour entry must precede every 5-minute one"


def test_a_tool_loop_caches_its_conversation_and_a_corrective_turn_keeps_the_prefix(monkeypatch):
    from services.registry.app.maintenance import activities

    seen = []
    m = activities.LiveActivityModel(_settings(monkeypatch), transport=_transport([_reply('{"action": "submit", "result": {}}')], seen))
    asyncio.run(m.complete([{"role": "system", "content": "json only"}, {"role": "user", "content": "go"}], max_tokens=2000))
    assert seen[0]["cache_control"] == {"type": "ephemeral"} and seen[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    # decide: a reply that is not a decision gets ONE corrective turn; the note lands after the last breakpoint
    seen = []
    model = AnthropicModel(_settings(monkeypatch), transport=_transport([_reply("<not json>"), _reply()], seen))
    asyncio.run(model.decide(_ctx()))
    assert _cached_prefix(seen[0]) == _cached_prefix(seen[1])
    assert "PREVIOUS REPLY WAS REJECTED" in seen[1]["messages"][0]["content"][-1]["text"]


# ── exact cost: reads, 5-minute and 1-hour writes ──────────────────────


def test_price_table_matches_the_official_multipliers():
    # https://platform.claude.com/docs/en/about-claude/pricing (Prompt caching): writes 1.25x (5m) and 2x (1h);
    # reads 0.1x, 0.05x on Claude Opus 5.5 and Claude Sonnet 5.5, 0.025x on Claude Fable 5.1
    assert (ANTHROPIC_CACHE_WRITE_X, ANTHROPIC_CACHE_WRITE_1H_X, ANTHROPIC_CACHE_READ_X_DEFAULT) == ("1.25", "2", "0.1")
    assert ANTHROPIC_CACHE_READ_X == {"claude-fable-5-1": "0.025", "claude-opus-5-5": "0.05", "claude-sonnet-5-5": "0.05"}
    per_mtok = {m: Decimal(p[0]) * 1000 for m, p in ANTHROPIC_USD_PER_1K.items()}
    assert per_mtok["claude-sonnet-5-5"] * Decimal(ANTHROPIC_CACHE_READ_X["claude-sonnet-5-5"]) == Decimal("0.10")  # $0.10 / MTok
    assert per_mtok["claude-sonnet-5"] * Decimal(ANTHROPIC_CACHE_READ_X_DEFAULT) == Decimal("0.20")  # $0.20 / MTok


@pytest.mark.parametrize("model, expected", [
    # 1000 uncached + 3000 read + 2000 5m-write + 4000 1h-write input, 200 out
    ("claude-sonnet-5-5", Decimal("0.002") + Decimal("0.0003") + Decimal("0.005") + Decimal("0.016") + Decimal("0.002")),
    ("claude-opus-5-5", Decimal("0.004") + Decimal("0.0006") + Decimal("0.010") + Decimal("0.032") + Decimal("0.004")),
    ("claude-sonnet-5", Decimal("0.002") + Decimal("0.0006") + Decimal("0.005") + Decimal("0.016") + Decimal("0.002")),
    ("claude-fable-5-1", Decimal("0.010") + Decimal("0.00075") + Decimal("0.025") + Decimal("0.080") + Decimal("0.010")),
])
def test_outcome_cost_prices_each_ttl(monkeypatch, model, expected):
    m = AnthropicModel(_settings(monkeypatch))
    out = ChatOutcome(content="{}", tokens_in=10000, tokens_out=200, tokens_cached=3000, tokens_cache_write=6000, tokens_cache_write_1h=4000)
    assert m.outcome_cost(model, out) == expected


def test_usage_by_ttl_reaches_the_response(monkeypatch):
    usage = {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 3000, "cache_creation_input_tokens": 6000,
             "cache_creation": {"ephemeral_5m_input_tokens": 2000, "ephemeral_1h_input_tokens": 4000}}
    r = asyncio.run(AnthropicModel(_settings(monkeypatch), transport=_transport([_reply(usage=usage)], [])).decide(_ctx()))
    assert (r.tokens_in, r.tokens_cached, r.tokens_cache_write, r.tokens_cache_write_1h) == (10000, 3000, 6000, 4000)
    assert r.cost_usd == Decimal("0.025300")
    # an older reply without the breakdown: every write is a 5-minute write (what the request asked for before)
    legacy = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 4000, "cache_read_input_tokens": 0}
    r2 = asyncio.run(AnthropicModel(_settings(monkeypatch), transport=_transport([_reply(usage=legacy)], [])).decide(_ctx()))
    assert (r2.tokens_cache_write, r2.tokens_cache_write_1h, r2.cost_usd) == (4000, 0, Decimal("0.010000"))


def test_an_activity_reply_carries_its_cache_tokens(monkeypatch):
    from services.registry.app.maintenance import activities

    usage = {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 50}
    m = activities.LiveActivityModel(_settings(monkeypatch), transport=_transport([_reply('{"action": "submit", "result": {}}', usage=usage)], []))
    reply = asyncio.run(m.complete([{"role": "system", "content": "json only"}, {"role": "user", "content": "go"}], max_tokens=2000))
    assert (reply.tokens_in, reply.tokens_cached, reply.tokens_cache_write) == (1050, 900, 50)


# ── the measurement: validator ``costs`` ───────────────────────────────


def _p5():
    spec = importlib.util.spec_from_file_location("phase5_live_cache", REPO / "deploy/railway/phase5_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cache_summary_per_role_model_window_and_day():
    p5 = _p5()
    now = NOW
    # (role, model, is_build, created_at, tokens_in, cache_read, cache_write|None, cost)
    runs = [
        ("scout", "claude-sonnet-5-5", False, now - timedelta(hours=2), 27000, 0, None, 0.060),
        ("scout", "claude-sonnet-5-5", False, now - timedelta(hours=1), 27000, 18000, 2000, 0.025),
        ("scout", "claude-sonnet-5-5", False, now - timedelta(days=3), 27000, 0, None, 0.060),
        ("builder", "claude-opus-5-5", True, now - timedelta(hours=5), 90000, 60000, 5000, 0.30),
    ]
    acts = [("AuthorPatch", "claude-sonnet-5-5", now - timedelta(hours=3), 50000, 40000, 3000, 0.04)]
    s = p5.cache_summary(runs, acts, now)
    day = s["decide"]["scout/claude-sonnet-5-5"]["24h"]
    assert day == {"runs": 2, "input": 54000, "cache_read": 18000, "cache_write": 2000, "write_unrecorded": 1,
                   "uncached": 34000, "hit_rate": 0.3333, "usd": 0.085, "usd_per_run": 0.0425}
    assert s["decide"]["scout/claude-sonnet-5-5"]["7d"]["runs"] == 3
    assert s["builds"]["claude-opus-5-5"]["24h"]["hit_rate"] == round(60000 / 90000, 4)
    assert s["activities"]["AuthorPatch/claude-sonnet-5-5"]["24h"]["cache_read"] == 40000
    assert s["decide_days"][str((now - timedelta(days=3)).date())]["scout"] == {"runs": 1, "hit_rate": 0.0, "usd_per_run": 0.06}
    # Sonnet/Opus decide runs whose writes were recorded (after this change): the >=50% target
    assert s["target"] == {"runs": 1, "hit_rate": round(18000 / 27000, 4), "goal": 0.5}
    # cadence: the median gap between a role's runs, and the TTL it implies
    # (gaps 60 min and 4200 min: the upper median; > 1h apart, neither TTL survives the gap)
    assert s["cadence"]["scout"] == {"runs": 3, "p50_gap_min": 4200.0, "ttl": "cold"}
    assert p5.ttl_for_gap(4.0) == "5m" and p5.ttl_for_gap(30.0) == "1h" and p5.ttl_for_gap(None) == "1h"


def test_the_costs_step_prints_the_cache_view(db, engine, capsys):
    import uuid

    from services.registry.app.models import AgentRun
    from services.registry.app.society.events import EventType, emit_event, utcnow
    from services.registry.app.society.seed import seed_society

    p5 = _p5()
    report = seed_society(db)
    for cached, write in ((0, None), (20000, 1500)):
        ev = emit_event(db, event_type=EventType.SOCIETY_HEARTBEAT, payload={}, idempotency_key=f"hb:{uuid.uuid4()}")
        db.add(AgentRun(id=uuid.uuid4(), agent_id=report.agents["scout"], event_id=ev.id, correlation_id=ev.correlation_id, role="scout",
                        status="completed", completed_at=utcnow(), cost_usd=Decimal("0.03"), model_provider="anthropic", model_name="claude-sonnet-5-5",
                        tokens_in=27000, tokens_out=500, tokens_cached=cached, tokens_cache_write=write))
    db.commit()
    out, conn = p5.Out(), engine.raw_connection()
    try:
        p5.step_costs(out, conn, 1, 15.0)
    finally:
        conn.close()
    printed = capsys.readouterr().out
    assert "PHASE5-JSON costs.cache_decide" in printed and '"hit_rate":0.3704' in printed
    assert "costs INFO C03 n/a" in printed or "costs C03" in printed, printed


def test_the_daily_report_states_the_cache_hit_rate():
    from services.registry.app.society import daily_report

    line = daily_report.cache_line({"runs": 10, "input": 270000, "cache_read": 150000, "hit_rate": 0.5556, "usd_per_run": 0.031})
    assert line == "Prompt cache (24h, decide runs): 55.6% of 270,000 input tokens read from cache; $0.031 per run"
    assert daily_report.cache_line({"runs": 0}) == "Prompt cache (24h, decide runs): no priced runs"
