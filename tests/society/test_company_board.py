"""The board: the owner's standing delegation for the daily plan (tickets.board_decide).

A plan the owner left undecided for SOCIETY_COMPANY_BOARD_APPROVAL_HOURS is approved
by deterministic code -- only while the company is healthy -- so the company does not
stand still waiting for the owner. Off by default; the owner decides first; it never
rejects, and it never touches the RED merge law."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from services.registry.app.models import AgentRun, SocietyEvent
from services.registry.app.society import company as company_mod
from services.registry.app.society import tickets
from services.registry.app.society.config import SocietyConfigError, SocietySettings, validate_settings
from services.registry.app.society.events import EventType, emit_event, utcnow
from services.registry.app.society.worker import SocietyWorker

from .test_company_tickets import _activate, company  # noqa: F401 -- fixture
from .test_red_tickets import _proposed


def _plan(db):
    _activate(db)
    t = _proposed(db, effect=0.01, department_role="qa")
    plan = tickets.build_plan(db, None, utcnow())
    db.commit()
    assert plan["status"] == "awaiting_owner"
    return uuid.UUID(plan["id"]), t


def _row(db, pid):
    return db.execute(text("SELECT status, decided_by, decided_by_user_id, board_note FROM society_daily_plans WHERE id = :id"), {"id": pid}).mappings().one()


def _board(settings, hours=6):
    return dataclasses.replace(settings, company_board_approval_hours=hours)


def _run(db, report, *, status="completed", cost="0"):
    agent_id = report.agents["scout"]
    ev = emit_event(db, event_type=EventType.SOCIETY_HEARTBEAT, payload={}, idempotency_key=f"hb:{uuid.uuid4()}")
    db.add(AgentRun(id=uuid.uuid4(), agent_id=agent_id, event_id=ev.id, correlation_id=ev.correlation_id, role="scout", status=status,
                    completed_at=utcnow(), cost_usd=Decimal(cost)))
    db.commit()


def test_the_board_is_off_by_default_and_every_plan_waits_for_the_owner(db, company):
    _, settings = company
    pid, _ = _plan(db)
    assert settings.company_board_approval_hours == 0
    assert tickets.board_decide(db, settings, utcnow() + timedelta(days=30)) == 0
    assert _row(db, pid)["status"] == "awaiting_owner"


def test_the_owner_window_comes_first_then_the_board_approves_and_wakes_the_architect(db, company):
    _, settings = company
    board = _board(settings)
    pid, t = _plan(db)
    assert tickets.board_decide(db, board, utcnow() + timedelta(hours=5)) == 0  # still the owner's window
    assert _row(db, pid)["status"] == "awaiting_owner"

    assert tickets.board_decide(db, board, utcnow() + timedelta(hours=7)) == 1
    db.commit()
    row = _row(db, pid)
    assert (row["status"], row["decided_by"], row["decided_by_user_id"]) == ("approved", "board", None)
    assert "undecided for 6h" in row["board_note"]
    assert tickets.for_proposal(db, t["proposal_id"])["status"] == "approved"
    ev = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.COMPANY_TICKET_APPROVED).one()
    assert ev.actor_type == "system" and ev.payload["approved_by"] == "board" and ev.payload["ticket_id"] == str(t["id"])
    # once decided, never again
    assert tickets.board_decide(db, board, utcnow() + timedelta(hours=30)) == 0
    assert tickets.plans_view(db)[0]["decided_by"] == "board"


def test_an_owner_decision_inside_the_window_is_final(db, company):
    _, settings = company
    pid, t = _plan(db)
    tickets.decide_plan(db, pid, approve=False, user_id=None)
    assert tickets.board_decide(db, _board(settings), utcnow() + timedelta(hours=30)) == 0
    row = _row(db, pid)
    assert (row["status"], row["decided_by"]) == ("rejected", "owner")
    assert tickets.for_proposal(db, t["proposal_id"])["status"] == "closed"


def test_an_incident_freeze_holds_the_board_until_an_operator_lifts_it(db, company):
    _, settings = company
    pid, _ = _plan(db)
    inc = company_mod.open_incident(db, reason="payments look wrong", source="test")
    later = utcnow() + timedelta(hours=7)
    assert tickets.board_decide(db, _board(settings), later) == 0
    db.commit()
    row = _row(db, pid)
    assert row["status"] == "awaiting_owner" and row["board_note"].startswith("board holds: incident freeze open (1)")
    db.execute(text("UPDATE society_incident_freezes SET lifted_at = NOW() WHERE id = :id"), {"id": inc.id})
    db.commit()
    assert tickets.board_decide(db, _board(settings), later) == 1


def test_recent_dead_runs_or_a_spent_budget_hold_the_board(db, company):
    report, settings = company
    pid, _ = _plan(db)
    board = dataclasses.replace(_board(settings), daily_model_budget_usd=Decimal("1.0"))
    db.execute(text("UPDATE society_daily_plans SET created_at = NOW() - INTERVAL '7 hours' WHERE id = :id"), {"id": pid})
    _run(db, report, status="dead")
    assert tickets.board_decide(db, board, utcnow()) == 0
    assert "DEAD society run(s)" in _row(db, pid)["board_note"]
    db.execute(text("DELETE FROM agent_runs"))
    _run(db, report, cost="1.5")
    assert tickets.board_decide(db, board, utcnow()) == 0
    assert "daily model budget spent (1 USD)" in _row(db, pid)["board_note"]
    db.execute(text("DELETE FROM agent_runs"))
    db.commit()
    assert tickets.board_hold_reason(db, board, utcnow()) is None


def test_the_worker_controllers_run_the_board(db, company, SessionLocal):
    _, settings = company
    pid, _ = _plan(db)
    db.execute(text("UPDATE society_daily_plans SET created_at = NOW() - INTERVAL '7 hours' WHERE id = :id"), {"id": pid})
    db.commit()
    SocietyWorker(SessionLocal, settings=_board(settings)).process_controllers()
    db.expire_all()
    assert _row(db, pid)["decided_by"] == "board"


def test_a_board_hold_is_recorded_and_a_failing_controller_never_disables_the_board(db, company, SessionLocal, monkeypatch, caplog):
    report, settings = company
    pid, _ = _plan(db)
    db.execute(text("UPDATE society_daily_plans SET created_at = NOW() - INTERVAL '7 hours' WHERE id = :id"), {"id": pid})
    db.commit()
    _run(db, report, status="dead")
    worker = SocietyWorker(SessionLocal, settings=_board(settings))
    worker.process_controllers()
    db.expire_all()
    row = _row(db, pid)  # the hold reaches the plan (it used to be rolled back with the step chain)
    assert row["status"] == "awaiting_owner" and "DEAD society run(s)" in row["board_note"]

    db.execute(text("DELETE FROM agent_runs"))
    db.commit()

    def broken(*_a, **_k):
        raise RuntimeError("broken sweep")

    monkeypatch.setattr(tickets, "sweep_stale_designs", broken)
    with caplog.at_level("ERROR"):
        worker.process_controllers()
    db.expire_all()
    assert _row(db, pid)["decided_by"] == "board"
    assert "company controller step sweep_stale_designs failed" in caplog.text


def test_the_board_window_is_bounded(monkeypatch):
    monkeypatch.setenv("SOCIETY_COMPANY_BOARD_APPROVAL_HOURS", "73")
    with pytest.raises(SocietyConfigError, match="SOCIETY_COMPANY_BOARD_APPROVAL_HOURS must be 0..72"):
        SocietySettings()
    monkeypatch.setenv("SOCIETY_COMPANY_BOARD_APPROVAL_HOURS", "12")
    assert SocietySettings().company_board_approval_hours == 12
    assert not any("BOARD" in p for p in validate_settings(SocietySettings()))


def test_the_validator_autonomy_step_reads_who_decided_and_how_long_plans_waited(db, company, engine, capsys):
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location("phase5_live_autonomy", pathlib.Path(__file__).resolve().parents[2] / "deploy/railway/phase5_live.py")
    p5 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p5)
    _, settings = company
    pid, _ = _plan(db)
    db.execute(text("UPDATE society_daily_plans SET created_at = NOW() - INTERVAL '30 hours' WHERE id = :id"), {"id": pid})
    db.commit()
    out, conn = p5.Out(), engine.raw_connection()
    try:
        p5.step_autonomy(out, conn, 7)
        assert out.failed == ["autonomy.A01"], "a plan undecided for 30h means the company waits for someone"
        assert tickets.board_decide(db, _board(settings), utcnow()) == 1
        db.commit()
        out = p5.Out()
        p5.step_autonomy(out, conn, 7)
    finally:
        conn.close()
    assert out.failed == []
    printed = capsys.readouterr().out
    assert "plans in 7d: {'board': 1}" in printed
    assert "listing requests: none yet (needs A2A_SERVER_ENABLED + A2A_PUBLIC_BASE_URL" in printed and "autonomy INFO" not in printed
    assert "PHASE5 autonomy A06 PASS bench tickets vs their newest reports" in printed
