"""Chatter-loop / runaway prevention with two agents that always reply
to each other. Without guards this would run forever."""

from __future__ import annotations

import asyncio

from services.registry.app.models import AgentCapabilityGrant, AgentChat, AgentIntent, AgentRun, SocietyEvent
from services.registry.app.society.cognition import FakeModel
from services.registry.app.society.config import SocietySettings, reset_settings_cache
from services.registry.app.society.events import EventType, emit_event
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _ping_pong(context):
    """Reply to whoever messaged me with a fresh message each time (the
    content includes the run id so repeated-message suppression cannot be
    what stops the loop — only the causation/correlation guards can)."""
    if context.event["type"] != EventType.AGENT_MESSAGE_RECEIVED:
        return {"decision_summary": "start", "intents": [{"type": "SEND_MESSAGE", "payload": {"to_agent": "Society_Architect", "title": "ping", "content": "start"}}]}
    sender = context.event["payload"]["data"]["from_agent"]
    return {"decision_summary": "reply", "intents": [{"type": "SEND_MESSAGE", "payload": {"to_agent": sender, "title": "pong", "content": f"reply from run {context.run_id}"}}], "sleep_for_seconds": 0}


def _settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    reset_settings_cache()
    return SocietySettings()


def test_chatter_loop_is_cut_by_causation_depth(db, SessionLocal, society_settings, monkeypatch, grants_with_no_cooldown):
    settings = _settings(monkeypatch, SOCIETY_MAX_CAUSATION_DEPTH=4, SOCIETY_MAX_RUNS_PER_CORRELATION=100, SOCIETY_REPEAT_MESSAGE_WINDOW_SECONDS=0)
    seed_society(db)
    grants_with_no_cooldown()
    model = FakeModel(_ping_pong)
    ev = emit_event(db, event_type="t.start")
    db.commit()
    worker = SocietyWorker(SessionLocal, settings=settings, model=model, worker_id="w")
    worker.routing = {"t.start": ["scout"]}
    stats = asyncio.run(worker.run_until_idle(max_cycles=60))
    runs = db.query(AgentRun).count()
    assert runs <= settings.max_causation_depth + 2, f"{runs} runs — loop not bounded"
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.LOOP_BREAKER_TRIPPED).count() == 1
    ignored = db.query(SocietyEvent).filter(SocietyEvent.status == "ignored", SocietyEvent.dispatch_note.like("loop breaker%")).count()
    assert ignored >= 1
    assert stats.loop_breaks >= 1
    assert ev.id is not None


def test_chatter_loop_is_cut_by_correlation_run_limit(db, SessionLocal, society_settings, monkeypatch, grants_with_no_cooldown):
    settings = _settings(monkeypatch, SOCIETY_MAX_CAUSATION_DEPTH=100, SOCIETY_MAX_RUNS_PER_CORRELATION=5, SOCIETY_REPEAT_MESSAGE_WINDOW_SECONDS=0)
    seed_society(db)
    grants_with_no_cooldown()
    model = FakeModel(_ping_pong)
    emit_event(db, event_type="t.start")
    db.commit()
    worker = SocietyWorker(SessionLocal, settings=settings, model=model, worker_id="w")
    worker.routing = {"t.start": ["scout"]}
    asyncio.run(worker.run_until_idle(max_cycles=60))
    assert db.query(AgentRun).count() <= 5
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.LOOP_BREAKER_TRIPPED).count() == 1


def test_repeated_identical_message_is_suppressed(db, SessionLocal, society_settings, grants_with_no_cooldown):
    seed_society(db)
    grants_with_no_cooldown()
    same = {"type": "SEND_MESSAGE", "payload": {"to_agent": "Society_Architect", "title": "same", "content": "same content"}}
    model = FakeModel({"Society_Scout": [{"decision_summary": "a", "intents": [same, same]}, {"decision_summary": "b", "intents": [same]}]})
    emit_event(db, event_type="t.a")
    emit_event(db, event_type="t.b")
    db.commit()
    worker = SocietyWorker(SessionLocal, settings=society_settings, model=model, worker_id="w")
    worker.routing = {"t.a": ["scout"], "t.b": ["scout"]}
    asyncio.run(worker.run_until_idle(max_cycles=20))
    assert db.query(AgentChat).count() == 1
    suppressed = [i for i in db.query(AgentIntent).all() if (i.result or {}).get("result", {}).get("suppressed") == "duplicate_message"]
    assert len(suppressed) == 2
    # only ONE agent.message.received event reached the Architect
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.AGENT_MESSAGE_RECEIVED).count() == 1


def test_global_runs_per_hour_limit(db, SessionLocal, monkeypatch, grants_with_no_cooldown, society_settings):
    settings = _settings(monkeypatch, SOCIETY_MAX_RUNS_PER_HOUR=2)
    seed_society(db)
    grants_with_no_cooldown()
    model = FakeModel([{"decision_summary": "n", "intents": []}] * 5)
    for i in range(4):
        emit_event(db, event_type="t.x", payload={"i": i})
    db.commit()
    worker = SocietyWorker(SessionLocal, settings=settings, model=model, worker_id="w")
    worker.routing = {"t.x": ["scout"]}
    asyncio.run(worker.run_until_idle(max_cycles=10))
    statuses = sorted(_ev(r.status) for r in db.query(AgentRun).all())
    assert statuses == ["completed", "completed", "skipped", "skipped"], statuses
    assert len(model.calls) == 2


def test_agent_with_failing_model_is_circuit_broken(db, SessionLocal, monkeypatch, grants_with_no_cooldown, society_settings):
    settings = _settings(monkeypatch, SOCIETY_RUN_MAX_ATTEMPTS=1, SOCIETY_CIRCUIT_BREAKER_FAILURES=2, SOCIETY_RETRY_BACKOFF_BASE_SECONDS=0)
    report = seed_society(db)
    grants_with_no_cooldown()
    model = FakeModel([RuntimeError("down")] * 10)
    for i in range(4):
        emit_event(db, event_type="t.x", payload={"i": i})
    db.commit()
    worker = SocietyWorker(SessionLocal, settings=settings, model=model, worker_id="w")
    worker.routing = {"t.x": ["scout"]}
    asyncio.run(worker.run_until_idle(max_cycles=10))
    grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == report.agents["scout"]).first()
    assert grant.paused_until is not None
    statuses = sorted(_ev(r.status) for r in db.query(AgentRun).all())
    assert statuses.count("dead") == 2 and statuses.count("skipped") == 2, statuses
    assert len(model.calls) == 2
