"""The runtime and the escrow layer: CREATE_TASK/START/COMPLETE/FAIL go
through task_service only. Wallet balances are touched only by the DB
trigger; the runtime never writes balance/cap fields.

Money invariant checked here: after an autonomous task completes, the
caller wallet lost exactly the escrow amount from balance (via trigger) and
holds no residual reservation; the callee gained escrow minus platform fee;
the sum of (balance + reserved) across both wallets never exceeds the
starting total. A refund path restores the caller fully.
"""

from __future__ import annotations

import asyncio

from services.registry.app.models import AgentIntent, AgentRun, TaskSession, Transaction, Wallet, WalletOwnerType
from services.registry.app.society.cognition import FakeModel
from services.registry.app.society.events import emit_event
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _wallet(db, agent_id):
    return db.query(Wallet).filter(Wallet.owner_type == WalletOwnerType.AGENT, Wallet.owner_id == agent_id).first()


def _run(db, SessionLocal, settings, model, event_type, routing, payload=None):
    ev = emit_event(db, event_type=event_type, payload=payload or {})
    db.commit()
    w = SocietyWorker(SessionLocal, settings=settings, model=model, worker_id="money")
    w.routing = routing
    asyncio.run(w.run_until_idle(max_cycles=20))
    return ev


def test_autonomous_task_lifecycle_respects_escrow_invariants(db, SessionLocal, society_settings, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    architect_id, builder_id = report.agents["architect"], report.agents["builder"]
    caller = _wallet(db, architect_id)
    caller.balance_credits = 100  # operator funding (dev), not the runtime
    db.commit()
    callee = _wallet(db, builder_id)
    start_total = caller.balance_credits + callee.balance_credits

    create = {"type": "CREATE_TASK", "payload": {"callee_agent": "Society_Builder", "capability": "implement_change", "input": {"candidate_id": "demo"}, "max_budget": 10, "timeout_seconds": 600}}
    model = FakeModel({"Society_Architect": [{"decision_summary": "delegate", "intents": [create]}]})
    _run(db, SessionLocal, society_settings, model, "t.a", {"t.a": ["architect"]})

    task = db.query(TaskSession).first()
    assert task is not None and _ev(task.status) == "initiated" and task.escrow_amount == 10
    db.refresh(caller)
    assert caller.balance_credits == 100 and caller.reserved_credits == 10, "escrow must be reserved, not spent"
    tx = db.query(Transaction).filter(Transaction.task_session_id == task.id).first()
    assert tx is not None and _ev(tx.status) == "pending"
    intent = db.query(AgentIntent).filter(AgentIntent.intent_type == "CREATE_TASK").first()
    assert tx.idempotency_key == intent.idempotency_key[:64]

    # Builder (callee) starts and completes through task_service; trigger moves the money.
    model2 = FakeModel({"Society_Builder": [
        {"decision_summary": "start", "intents": [{"type": "START_TASK", "payload": {"task_id": str(task.id)}}]},
        {"decision_summary": "done", "intents": [{"type": "COMPLETE_TASK", "payload": {"task_id": str(task.id), "output": {"ok": True}}}]},
    ]})
    _run(db, SessionLocal, society_settings, model2, "t.b1", {"t.b1": ["builder"]}, payload={"task_id": str(task.id)})
    _run(db, SessionLocal, society_settings, model2, "t.b2", {"t.b2": ["builder"]}, payload={"task_id": str(task.id)})
    db.expire_all()
    task = db.query(TaskSession).first()
    assert _ev(task.status) == "completed", [(i.intent_type, _ev(i.execution_status), i.error) for i in db.query(AgentIntent).all()]
    caller, callee = _wallet(db, architect_id), _wallet(db, builder_id)
    assert caller.reserved_credits == 0
    assert caller.balance_credits == 90
    assert 0 < callee.balance_credits <= 10  # platform fee may apply
    assert caller.balance_credits + callee.balance_credits <= start_total
    tx = db.query(Transaction).filter(Transaction.task_session_id == task.id).first()
    assert _ev(tx.status) == "completed"


def test_over_cap_task_is_denied_and_insufficient_funds_fail_safely(db, SessionLocal, society_settings, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    caller = _wallet(db, report.agents["architect"])
    caller.balance_credits = 5
    db.commit()
    model = FakeModel({"Society_Architect": [{"decision_summary": "spend", "intents": [
        {"type": "CREATE_TASK", "payload": {"callee_agent": "Society_Builder", "capability": "implement_change", "input": {"candidate_id": "x"}, "max_budget": 500}},
        {"type": "CREATE_TASK", "payload": {"callee_agent": "Society_Builder", "capability": "implement_change", "input": {"candidate_id": "x"}, "max_budget": 10}},
    ]}]})
    _run(db, SessionLocal, society_settings, model, "t.a", {"t.a": ["architect"]})
    intents = db.query(AgentIntent).filter(AgentIntent.intent_type == "CREATE_TASK").order_by(AgentIntent.seq).all()
    assert _ev(intents[0].execution_status) == "denied" and "escrow cap" in intents[0].policy_reason
    assert _ev(intents[1].execution_status) == "failed" and "escrow refused" in intents[1].error
    db.refresh(caller)
    assert caller.balance_credits == 5 and caller.reserved_credits == 0
    assert db.query(TaskSession).count() == 0 and db.query(Transaction).count() == 0


def test_failed_task_refunds_reservation(db, SessionLocal, society_settings, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    caller = _wallet(db, report.agents["architect"])
    caller.balance_credits = 50
    db.commit()
    model = FakeModel({"Society_Architect": [{"decision_summary": "delegate", "intents": [
        {"type": "CREATE_TASK", "payload": {"callee_agent": "Society_Builder", "capability": "implement_change", "input": {"candidate_id": "x"}, "max_budget": 20}}]}]})
    _run(db, SessionLocal, society_settings, model, "t.a", {"t.a": ["architect"]})
    task = db.query(TaskSession).first()
    model2 = FakeModel({"Society_Builder": [
        {"decision_summary": "start", "intents": [{"type": "START_TASK", "payload": {"task_id": str(task.id)}}]},
        {"decision_summary": "fail", "intents": [{"type": "FAIL_TASK", "payload": {"task_id": str(task.id), "error": "could not implement"}}]},
    ]})
    _run(db, SessionLocal, society_settings, model2, "t.b1", {"t.b1": ["builder"]})
    _run(db, SessionLocal, society_settings, model2, "t.b2", {"t.b2": ["builder"]})
    db.expire_all()
    task = db.query(TaskSession).first()
    assert _ev(task.status) == "failed"
    caller = _wallet(db, report.agents["architect"])
    assert caller.balance_credits == 50 and caller.reserved_credits == 0
    # task.failed event was emitted for the Scout to learn from
    from services.registry.app.models import SocietyEvent

    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == "task.failed").count() == 1
    assert db.query(AgentRun).count() >= 3
