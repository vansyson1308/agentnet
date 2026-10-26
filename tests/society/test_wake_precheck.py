"""Heartbeat precheck (wake_precheck.py): an idle heartbeat wake calls no model; real work and liveness still wake."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import timedelta

from sqlalchemy import text

from services.registry.app.models import AgentRun, AgentRunStatus, SocietyEvent
from services.registry.app.society import company as company_mod, wake_precheck
from services.registry.app.society.events import EventType, emit_event, utcnow
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker


class _NoModel:
    provider, model_name = "none", "none"
    calls = 0

    async def decide(self, context):
        _NoModel.calls += 1
        raise AssertionError("an idle heartbeat must not reach the model")


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _recently_active(db, report, roles, hours_ago=1):
    """Each role completed a run recently (else the liveness rule wakes it)."""
    seed = emit_event(db, event_type=EventType.SOCIETY_HEARTBEAT_IDLE, payload={}, actor_type="system", idempotency_key=f"seed:{uuid.uuid4()}")
    at = utcnow() - timedelta(hours=hours_ago)
    for role in roles:
        db.add(AgentRun(id=uuid.uuid4(), agent_id=report.agents[role], event_id=seed.id, correlation_id=seed.correlation_id or seed.id, role=role, status=AgentRunStatus.COMPLETED,
                        completed_at=at, cost_usd=0.05))
    db.commit()


def _heartbeat(db):
    emit_event(db, event_type=EventType.SOCIETY_HEARTBEAT, payload={"bucket": 1}, actor_type="system", idempotency_key=f"heartbeat:{uuid.uuid4()}")
    db.commit()


def _heartbeat_runs(db):
    hb = db.query(SocietyEvent.id).filter(SocietyEvent.event_type == EventType.SOCIETY_HEARTBEAT)
    return {r.role: r for r in db.query(AgentRun).filter(AgentRun.event_id.in_(hb)).all()}


def test_an_idle_heartbeat_is_skipped_before_any_model_call(db, SessionLocal, society_settings, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    _recently_active(db, report, ("governor", "builder"))
    _heartbeat(db)
    settings = dataclasses.replace(society_settings, heartbeat_precheck_enabled=True, heartbeat_interval_seconds=0)
    _NoModel.calls = 0
    asyncio.run(SocietyWorker(SessionLocal, settings=settings, model=_NoModel(), worker_id="w").run_until_idle(max_cycles=5))
    db.expire_all()
    runs = _heartbeat_runs(db)
    assert {"governor", "builder"} <= set(runs) and _NoModel.calls == 0
    assert all(_ev(r.status) == "skipped" and r.error.startswith(wake_precheck.IDLE_PREFIX) and float(r.cost_usd or 0) == 0 for r in runs.values())
    saved = company_mod.status_report(db, settings)["heartbeat_precheck"]
    assert saved["enabled"] and saved["idle_skips_24h"] == len(runs)


def test_work_in_a_roles_stage_or_a_long_silence_still_wakes_it(db, society_settings):
    report = seed_society(db)
    now = utcnow()
    _recently_active(db, report, ("governor", "builder", "scout"))
    for role in ("governor", "builder", "scout"):
        assert wake_precheck.heartbeat_work(db, agent_id=report.agents[role], role=role, now=now, max_idle_hours=6)[0] is False, role

    db.execute(text("INSERT INTO improvement_proposals (id, source, title, problem, proposed_change, status, target_scope, importance) "
                    "VALUES (gen_random_uuid(), 'audit', 't', 'p', 'c', 'PROPOSED', 'platform', 50)"))
    emit_event(db, event_type=EventType.BACKLOG_ITEM, payload={"key": "x"}, actor_type="system", idempotency_key="backlog:x")
    db.commit()
    busy, why = wake_precheck.heartbeat_work(db, agent_id=report.agents["governor"], role="governor", now=now, max_idle_hours=6)
    assert busy and "unreviewed proposal" in why
    busy, why = wake_precheck.heartbeat_work(db, agent_id=report.agents["scout"], role="scout", now=now, max_idle_hours=6)
    assert busy and "backlog item" in why
    busy, why = wake_precheck.heartbeat_work(db, agent_id=report.agents["builder"], role="builder", now=now + timedelta(hours=7), max_idle_hours=6)
    assert busy and why.startswith("liveness"), "a precheck gap can delay work, never strand it"


def test_the_precheck_is_off_by_default(society_settings):
    assert society_settings.heartbeat_precheck_enabled is False and society_settings.heartbeat_max_idle_hours == 6
