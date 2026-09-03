"""World-signal ingestion + heartbeat."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from services.registry.app.models import AgentRun, ImprovementProposal, SocietyEvent, TaskSession, TaskStatus
from services.registry.app.society.cognition import ScriptedRoleModel
from services.registry.app.society.config import SocietySettings, reset_settings_cache
from services.registry.app.society.events import EventType, utcnow
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker
from services.registry.app.society.world import emit_heartbeat, ingest_task_outcomes


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _failed_task(db, caller, callee, capability="translate", status=TaskStatus.FAILED, minutes_ago=5):
    t = TaskSession(
        id=uuid.uuid4(),
        trace_id=uuid.uuid4(),
        span_id=uuid.uuid4(),
        caller_agent_id=caller,
        callee_agent_id=callee,
        capability=capability,
        input={"x": 1},
        escrow_amount=0,
        status=status,
        timeout_at=utcnow(),
        completed_at=utcnow() - timedelta(minutes=minutes_ago),
        error_message="upstream 500",
    )
    db.add(t)
    db.commit()
    return t


def test_ingest_emits_one_event_per_failed_task_and_is_idempotent(db, make_agent, society_settings):
    a, b = make_agent("A"), make_agent("B")
    t1 = _failed_task(db, a.id, b.id)
    t2 = _failed_task(db, a.id, b.id, status=TaskStatus.TIMEOUT)
    _failed_task(db, a.id, b.id, minutes_ago=600)  # outside lookback
    assert ingest_task_outcomes(db, lookback_seconds=3600) == 2
    assert ingest_task_outcomes(db, lookback_seconds=3600) == 0
    events = db.query(SocietyEvent).all()
    assert {(e.event_type, e.subject_id) for e in events} == {(EventType.TASK_FAILED, t1.id), (EventType.TASK_TIMEOUT, t2.id)}
    assert all(e.correlation_id == t.trace_id for e, t in zip(sorted(events, key=lambda e: e.event_type), [t1, t2]))


def test_ingested_failure_wakes_scout_which_proposes(db, SessionLocal, make_agent, society_settings, grants_with_no_cooldown):
    seed_society(db)
    grants_with_no_cooldown()
    a, b = make_agent("Ext_A"), make_agent("Ext_B")
    _failed_task(db, a.id, b.id, capability="summarise")
    worker = SocietyWorker(SessionLocal, settings=society_settings, model=ScriptedRoleModel(), worker_id="w")
    asyncio.run(worker.run_until_idle(max_cycles=20))
    runs = db.query(AgentRun).all()
    assert any(r.role == "scout" and _ev(r.status) == "completed" for r in runs)
    props = db.query(ImprovementProposal).all()
    assert len(props) == 1 and props[0].title == "Improve: summarise"


def test_heartbeat_is_bounded_per_interval(db, monkeypatch, society_settings):
    monkeypatch.setenv("SOCIETY_HEARTBEAT_INTERVAL_SECONDS", "3600")
    reset_settings_cache()
    settings = SocietySettings()
    assert emit_heartbeat(db, settings) is True
    assert emit_heartbeat(db, settings) is False
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.SOCIETY_HEARTBEAT).count() == 1
    later = utcnow() + timedelta(hours=2)
    assert emit_heartbeat(db, settings, now=later) is True
    monkeypatch.setenv("SOCIETY_HEARTBEAT_INTERVAL_SECONDS", "0")
    reset_settings_cache()
    assert emit_heartbeat(db, SocietySettings()) is False


def test_heartbeat_wakes_governor_to_create_society_goal(db, SessionLocal, monkeypatch, society_settings, grants_with_no_cooldown):
    monkeypatch.setenv("SOCIETY_HEARTBEAT_INTERVAL_SECONDS", "3600")
    reset_settings_cache()
    settings = SocietySettings()
    seed_society(db)
    grants_with_no_cooldown()
    worker = SocietyWorker(SessionLocal, settings=settings, model=ScriptedRoleModel(), worker_id="w")
    asyncio.run(worker.run_until_idle(max_cycles=10))
    from services.registry.app.models import Goal, GoalOwnerType

    goals = db.query(Goal).filter(Goal.owner_type == GoalOwnerType.SOCIETY).all()
    assert len(goals) == 1 and "reliable" in goals[0].title
    assert db.query(AgentRun).filter(AgentRun.role == "governor").count() == 1
