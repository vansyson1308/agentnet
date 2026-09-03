"""Durable events + dispatch rules (real Postgres)."""

from __future__ import annotations

import uuid
from datetime import timedelta

from services.registry.app.models import AgentRun, SocietyEvent
from services.registry.app.society.events import EventType, emit_event, expire_stale_events, utcnow
from services.registry.app.society.roles import subscriptions_by_event
from services.registry.app.society.runs import dispatch_pending_events
from services.registry.app.society.seed import seed_society


def _ev(v):
    return v.value if hasattr(v, "value") else v


def test_emit_event_is_durable_and_dedupes_on_idempotency_key(db):
    a = emit_event(db, event_type="x.test", payload={"n": 1}, idempotency_key="k1")
    db.commit()
    b = emit_event(db, event_type="x.test", payload={"n": 2}, idempotency_key="k1")
    db.commit()
    assert a.id == b.id
    assert db.query(SocietyEvent).count() == 1
    row = db.query(SocietyEvent).first()
    assert row.payload == {"n": 1}
    assert _ev(row.status) == "pending"
    assert row.correlation_id is not None and row.trace_id == row.correlation_id


def test_causation_chain_increments_depth_and_shares_correlation(db):
    root = emit_event(db, event_type="root")
    child = emit_event(db, event_type="child", causation=root)
    grandchild = emit_event(db, event_type="grandchild", causation=child)
    db.commit()
    assert child.correlation_id == root.correlation_id == grandchild.correlation_id
    assert (root.causation_depth, child.causation_depth, grandchild.causation_depth) == (0, 1, 2)
    assert grandchild.causation_id == child.id


def test_expire_stale_events(db):
    old = emit_event(db, event_type="old")
    db.commit()
    old.created_at = utcnow() - timedelta(hours=48)
    db.commit()
    fresh = emit_event(db, event_type="fresh")
    db.commit()
    n = expire_stale_events(db, ttl_seconds=3600)
    db.commit()
    assert n == 1
    db.refresh(old)
    db.refresh(fresh)
    assert _ev(old.status) == "expired" and _ev(fresh.status) == "pending"


def test_dispatch_routes_by_role_and_never_to_the_actor_itself(db, society_settings):
    report = seed_society(db)
    routing = subscriptions_by_event(__import__("services.registry.app.society.roles", fromlist=["DEFAULT_ROLES"]).DEFAULT_ROLES)
    # Scout emits a proposal.created event: Governor subscribes; Scout must not be woken by its own event.
    ev = emit_event(db, event_type=EventType.PROPOSAL_CREATED, actor_type="agent", actor_id=report.agents["scout"], payload={})
    db.commit()
    stats = dispatch_pending_events(db, settings=society_settings, routing=routing)
    assert stats.events_dispatched == 1 and stats.runs_created == 1
    runs = db.query(AgentRun).filter(AgentRun.event_id == ev.id).all()
    assert [r.role for r in runs] == ["governor"]
    db.refresh(ev)
    assert _ev(ev.status) == "dispatched"


def test_dispatch_targets_a_specific_agent_even_if_it_is_the_actor(db, society_settings):
    report = seed_society(db)
    routing = {}
    ev = emit_event(
        db,
        event_type=EventType.AGENT_MESSAGE_RECEIVED,
        actor_type="agent",
        actor_id=report.agents["scout"],
        subject_type="agent",
        subject_id=report.agents["scout"],
        payload={"target_agent_id": str(report.agents["scout"])},
    )
    db.commit()
    stats = dispatch_pending_events(db, settings=society_settings, routing=routing)
    assert stats.runs_created == 1
    assert db.query(AgentRun).filter(AgentRun.event_id == ev.id, AgentRun.agent_id == report.agents["scout"]).count() == 1


def test_dispatch_without_subscriber_marks_event_ignored(db, society_settings):
    seed_society(db)
    ev = emit_event(db, event_type="nobody.cares")
    db.commit()
    stats = dispatch_pending_events(db, settings=society_settings, routing={})
    db.refresh(ev)
    assert stats.events_ignored == 1 and _ev(ev.status) == "ignored" and ev.dispatch_note == "no subscriber"


def test_unique_agent_event_prevents_double_dispatch(db, society_settings):
    report = seed_society(db)
    routing = {"t.x": ["scout", "scout"]}  # duplicated subscription on purpose
    ev = emit_event(db, event_type="t.x")
    db.commit()
    stats = dispatch_pending_events(db, settings=society_settings, routing=routing)
    assert stats.runs_created == 1
    # simulate a replay: reset the event to pending and dispatch again
    ev.status = "pending"
    db.commit()
    stats2 = dispatch_pending_events(db, settings=society_settings, routing=routing)
    assert stats2.runs_created == 0 and stats2.duplicates_prevented >= 1
    assert db.query(AgentRun).filter(AgentRun.event_id == ev.id).count() == 1
    assert report.agents["scout"] == db.query(AgentRun).filter(AgentRun.event_id == ev.id).first().agent_id


def test_causation_depth_loop_breaker(db, society_settings, monkeypatch):
    seed_society(db)
    routing = {"t.deep": ["scout"]}
    root = emit_event(db, event_type="t.root")
    root.status = "processed"
    ev = emit_event(db, event_type="t.deep", causation=root)
    ev.causation_depth = society_settings.max_causation_depth + 1
    db.commit()
    stats = dispatch_pending_events(db, settings=society_settings, routing=routing)
    db.refresh(ev)
    assert stats.loop_breaks == 1 and _ev(ev.status) == "ignored"
    assert "causation depth" in ev.dispatch_note
    breaker = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.LOOP_BREAKER_TRIPPED).all()
    assert len(breaker) == 1 and breaker[0].correlation_id == ev.correlation_id and breaker[0].causation_depth == 0
    # a second breaker for the same correlation is deduped
    ev2 = emit_event(db, event_type="t.deep", causation=root)
    ev2.causation_depth = society_settings.max_causation_depth + 5
    db.commit()
    dispatch_pending_events(db, settings=society_settings, routing=routing)
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.LOOP_BREAKER_TRIPPED).count() == 1


def test_correlation_run_limit_loop_breaker(db, society_settings, monkeypatch):
    from services.registry.app.society.config import SocietySettings, reset_settings_cache

    monkeypatch.setenv("SOCIETY_MAX_RUNS_PER_CORRELATION", "2")
    reset_settings_cache()
    settings = SocietySettings()
    report = seed_society(db)
    routing = {"t.c": ["scout", "governor", "architect"]}
    corr = uuid.uuid4()
    e1 = emit_event(db, event_type="t.c", correlation_id=corr)
    db.commit()
    dispatch_pending_events(db, settings=settings, routing=routing)  # creates 3 runs (limit checked before dispatch)
    assert db.query(AgentRun).filter(AgentRun.correlation_id == corr).count() == 3
    e2 = emit_event(db, event_type="t.c", correlation_id=corr, causation=e1)
    db.commit()
    stats = dispatch_pending_events(db, settings=settings, routing=routing)
    db.refresh(e2)
    assert stats.loop_breaks == 1 and _ev(e2.status) == "ignored"
    assert db.query(AgentRun).filter(AgentRun.correlation_id == corr).count() == 3
    assert report.agents  # seeded
