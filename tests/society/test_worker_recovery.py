"""Worker failure recovery with the FakeModel: model timeouts, invalid
structured output, provider errors, intent execution failures, crash after
persisting intents (resume without re-deciding), duplicate events, and
budget/cooldown skips. All against Postgres; no live model."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from services.registry.app.models import AgentCapabilityGrant, AgentChat, AgentIntent, AgentRun, SocietyEvent
from services.registry.app.society.cognition import FakeModel
from services.registry.app.society.events import emit_event, utcnow
from services.registry.app.society.runs import claim_next_run
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _settings(monkeypatch, **env):
    from services.registry.app.society.config import SocietySettings, reset_settings_cache

    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    reset_settings_cache()
    return SocietySettings()


def _emit_and_run(db, SessionLocal, settings, model, event_type="t.scout", payload=None, routing=None, **kw):
    ev = emit_event(db, event_type=event_type, payload=payload or {}, **kw)
    db.commit()
    worker = SocietyWorker(SessionLocal, settings=settings, model=model, worker_id="w-test")
    if routing is not None:
        worker.routing = routing
    else:
        worker.routing = {"t.scout": ["scout"]}
    stats = asyncio.run(worker.run_until_idle(max_cycles=20))
    return ev, worker, stats


def test_model_timeout_is_retried_then_dead(db, SessionLocal, society_settings, monkeypatch, grants_with_no_cooldown):
    settings = _settings(monkeypatch, SOCIETY_RUN_MAX_ATTEMPTS=2, SOCIETY_RETRY_BACKOFF_BASE_SECONDS=0, SOCIETY_CIRCUIT_BREAKER_FAILURES=5)
    seed_society(db)
    grants_with_no_cooldown()
    model = FakeModel([asyncio.TimeoutError(), asyncio.TimeoutError(), {"decision_summary": "never", "intents": []}])
    ev, worker, stats = _emit_and_run(db, SessionLocal, settings, model)
    run = db.query(AgentRun).filter(AgentRun.event_id == ev.id).first()
    assert _ev(run.status) == "dead", (run.status, run.error)
    assert run.attempt == 2 and "timeout" in run.error
    assert len(model.calls) == 2
    assert stats.runs_dead == 1


def test_invalid_structured_output_is_recorded_and_retried(db, SessionLocal, society_settings, monkeypatch, grants_with_no_cooldown):
    settings = _settings(monkeypatch, SOCIETY_RUN_MAX_ATTEMPTS=2, SOCIETY_RETRY_BACKOFF_BASE_SECONDS=0)
    seed_society(db)
    grants_with_no_cooldown()
    model = FakeModel(["this is not json", {"decision_summary": "ok now", "intents": [], "sleep_for_seconds": 5}])
    ev, worker, stats = _emit_and_run(db, SessionLocal, settings, model)
    run = db.query(AgentRun).filter(AgentRun.event_id == ev.id).first()
    assert _ev(run.status) == "completed" and run.attempt == 2
    assert run.decision_summary == "ok now" and run.model_provider == "fake"
    assert "invalid structured output" in (run.error or "")  # last error kept for the audit trail


def test_provider_exception_does_not_crash_worker(db, SessionLocal, society_settings, monkeypatch, grants_with_no_cooldown):
    settings = _settings(monkeypatch, SOCIETY_RUN_MAX_ATTEMPTS=1, SOCIETY_RETRY_BACKOFF_BASE_SECONDS=0)
    seed_society(db)
    grants_with_no_cooldown()
    model = FakeModel([RuntimeError("provider 503")])
    ev, worker, stats = _emit_and_run(db, SessionLocal, settings, model)
    run = db.query(AgentRun).filter(AgentRun.event_id == ev.id).first()
    assert _ev(run.status) == "dead" and "provider 503" in run.error


def test_intent_execution_failure_is_recorded_not_retried(db, SessionLocal, society_settings, grants_with_no_cooldown):
    seed_society(db)
    grants_with_no_cooldown()
    model = FakeModel(
        [
            {
                "decision_summary": "message a ghost",
                "intents": [
                    {"type": "SEND_MESSAGE", "payload": {"to_agent": "Nobody_Here", "title": "hi", "content": "x"}},
                    {"type": "WRITE_MEMORY", "payload": {"title": "still works", "content": "second intent executes", "scope": "agent"}},
                ],
                "sleep_for_seconds": 1,
            }
        ]
    )
    ev, worker, stats = _emit_and_run(db, SessionLocal, society_settings, model)
    run = db.query(AgentRun).filter(AgentRun.event_id == ev.id).first()
    assert _ev(run.status) == "completed"
    intents = db.query(AgentIntent).filter(AgentIntent.run_id == run.id).order_by(AgentIntent.seq).all()
    assert [_ev(i.execution_status) for i in intents] == ["failed", "executed"]
    assert "unknown agent" in intents[0].error
    assert len(model.calls) == 1


def test_crash_after_persisting_intents_resumes_without_second_model_call(db, SessionLocal, society_settings, monkeypatch, grants_with_no_cooldown):
    """Simulate a worker that decided + persisted intents, then died before
    executing them. The re-claimed run must execute the persisted intents
    once and must NOT call the model again."""
    settings = _settings(monkeypatch, SOCIETY_RETRY_BACKOFF_BASE_SECONDS=0)
    report = seed_society(db)
    grants_with_no_cooldown()
    model = FakeModel(
        [
            {
                "decision_summary": "one message",
                "intents": [{"type": "SEND_MESSAGE", "payload": {"to_agent": "Society_Architect", "title": "t", "content": "c"}}],
                "sleep_for_seconds": 1,
            }
        ]
    )
    ev = emit_event(db, event_type="t.scout")
    db.commit()
    worker = SocietyWorker(SessionLocal, settings=settings, model=model, worker_id="w1")
    worker.routing = {"t.scout": ["scout"]}
    worker.dispatch()
    run = claim_next_run(db, worker_id="w1", lease_seconds=60)
    # replicate the first half of process_run: context + decision persisted, nothing executed
    from services.registry.app.models import Agent
    from services.registry.app.society.context import build_context

    agent = db.query(Agent).filter(Agent.id == run.agent_id).first()
    grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == run.agent_id).first()
    ctx = build_context(db, agent=agent, grant=grant, event=ev, run=run, settings=settings)
    response = asyncio.run(model.decide(ctx))
    worker._persist_decision(db, run, agent, grant, ctx, response)
    assert db.query(AgentIntent).filter(AgentIntent.run_id == run.id).count() == 1
    assert db.query(AgentChat).count() == 0
    # crash: lease expires
    run.lease_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    asyncio.run(worker.run_until_idle(max_cycles=10))
    db.expire_all()
    run = db.query(AgentRun).filter(AgentRun.id == run.id).first()
    assert _ev(run.status) == "completed" and run.attempt == 2
    # the model was consulted exactly once for THIS run (the second call in
    # model.calls belongs to the Architect run woken by the delivered message)
    assert [c.run_id for c in model.calls].count(str(run.id)) == 1, "model was called again on resume"
    assert db.query(AgentChat).count() == 1
    intents = db.query(AgentIntent).filter(AgentIntent.run_id == run.id).all()
    assert len(intents) == 1 and _ev(intents[0].execution_status) == "executed"
    assert report.agents["architect"] == db.query(AgentChat).first().to_agent_id


def test_duplicate_event_produces_one_run(db, SessionLocal, society_settings, grants_with_no_cooldown):
    seed_society(db)
    grants_with_no_cooldown()
    model = FakeModel([{"decision_summary": "n", "intents": []}] * 3)
    emit_event(db, event_type="t.scout", idempotency_key="dup-1")
    emit_event(db, event_type="t.scout", idempotency_key="dup-1")
    emit_event(db, event_type="t.scout", idempotency_key="dup-1")
    db.commit()
    worker = SocietyWorker(SessionLocal, settings=society_settings, model=model, worker_id="w")
    worker.routing = {"t.scout": ["scout"]}
    asyncio.run(worker.run_until_idle(max_cycles=10))
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == "t.scout").count() == 1
    assert db.query(AgentRun).count() == 1 and len(model.calls) == 1


def test_run_skipped_when_runtime_disabled(db, SessionLocal, monkeypatch, grants_with_no_cooldown, society_settings):
    settings = _settings(monkeypatch, SOCIETY_RUNTIME_ENABLED="false")
    seed_society(db)
    grants_with_no_cooldown()
    model = FakeModel([{"decision_summary": "n", "intents": []}])
    ev, worker, stats = _emit_and_run(db, SessionLocal, settings, model)
    run = db.query(AgentRun).filter(AgentRun.event_id == ev.id).first()
    assert _ev(run.status) == "skipped" and "SOCIETY_RUNTIME_ENABLED" in run.error
    assert len(model.calls) == 0


def test_daily_budget_exhausted_skips_before_model_call(db, SessionLocal, monkeypatch, grants_with_no_cooldown, society_settings):
    settings = _settings(monkeypatch, SOCIETY_DAILY_MODEL_BUDGET="0.0001")
    seed_society(db)
    grants_with_no_cooldown()
    model = FakeModel([{"decision_summary": "n", "intents": []}] * 2, cost_usd="0.001")
    ev1, worker, _ = _emit_and_run(db, SessionLocal, settings, model)
    ev2, worker, _ = _emit_and_run(db, SessionLocal, settings, model)
    r1 = db.query(AgentRun).filter(AgentRun.event_id == ev1.id).first()
    r2 = db.query(AgentRun).filter(AgentRun.event_id == ev2.id).first()
    assert _ev(r1.status) == "completed" and float(r1.cost_usd) == 0.001
    assert _ev(r2.status) == "skipped" and "budget" in r2.error
    assert len(model.calls) == 1


def test_agent_cooldown_skips_second_wake(db, SessionLocal, society_settings, monkeypatch):
    seed_society(db)  # default cooldown 30s for scout
    model = FakeModel([{"decision_summary": "n", "intents": []}] * 2)
    ev1, worker, _ = _emit_and_run(db, SessionLocal, society_settings, model)
    ev2, worker, _ = _emit_and_run(db, SessionLocal, society_settings, model)
    r2 = db.query(AgentRun).filter(AgentRun.event_id == ev2.id).first()
    assert _ev(r2.status) == "skipped" and "cooldown" in r2.error
    assert len(model.calls) == 1


def test_max_intents_per_run_enforced(db, SessionLocal, society_settings, grants_with_no_cooldown):
    seed_society(db)
    grants_with_no_cooldown()
    many = [{"type": "WRITE_MEMORY", "payload": {"title": f"m{i}", "content": "c", "scope": "agent"}} for i in range(10)]
    model = FakeModel([{"decision_summary": "flood", "intents": many}])
    ev, worker, stats = _emit_and_run(db, SessionLocal, society_settings, model)
    run = db.query(AgentRun).filter(AgentRun.event_id == ev.id).first()
    grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == run.agent_id).first()
    intents = db.query(AgentIntent).filter(AgentIntent.run_id == run.id).all()
    executed = [i for i in intents if _ev(i.execution_status) == "executed"]
    assert len(executed) == min(grant.max_intents_per_run, society_settings.max_intents_per_run)
    assert any("max_intents_per_run" in (i.policy_reason or "") for i in intents)
