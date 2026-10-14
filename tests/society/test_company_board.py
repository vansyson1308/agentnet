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


def _phase5():
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location("phase5_live_autonomy", pathlib.Path(__file__).resolve().parents[2] / "deploy/railway/phase5_live.py")
    p5 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p5)
    return p5


def test_the_validator_autonomy_step_reads_who_decided_and_how_long_plans_waited(db, company, engine, capsys):
    p5 = _phase5()
    _, settings = company
    pid, _ = _plan(db)
    db.execute(text("UPDATE society_daily_plans SET created_at = NOW() - INTERVAL '30 hours' WHERE id = :id"), {"id": pid})
    db.commit()
    inc = company_mod.open_incident(db, reason="surface down", source="public_surface")
    out, conn = p5.Out(), engine.raw_connection()
    try:
        p5.step_autonomy(out, conn, 7)
        assert out.failed == ["autonomy.A01"], "a plan undecided for 30h means the company waits for someone"
        assert "open freezes (source, incident status, n, oldest h): [('public_surface', '-', 1," in capsys.readouterr().out
        db.execute(text("UPDATE society_incident_freezes SET lifted_at = NOW() WHERE id = :id"), {"id": inc.id})
        db.commit()
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


def _maint_incident(db, status):
    from services.registry.app.maintenance.orm import MaintenanceIncident

    inc = MaintenanceIncident(id=uuid.uuid4(), fingerprint=uuid.uuid4().hex, incident_class="AVAILABILITY", priority="P1", severity="major", source="probe",
                              target="production", desired_state_ref="surface:/", status=status, first_observed_at=utcnow(), last_observed_at=utcnow(),
                              current_evidence_digest="d" * 64)
    db.add(inc)
    db.flush()
    return company_mod.open_incident(db, reason=f"maintenance {status}", source="maintenance", evidence={"incident_id": str(inc.id)})


def test_freezes_that_outlived_their_incident_are_named_on_the_plan_and_only_they_are_lifted(db, company, engine, SessionLocal, monkeypatch, capsys):
    import types

    p5 = _phase5()
    _, settings = company
    pid, _ = _plan(db)
    db.execute(text("UPDATE society_daily_plans SET created_at = NOW() - INTERVAL '7 hours' WHERE id = :id"), {"id": pid})
    recovered, closed = _maint_incident(db, "recovered"), _maint_incident(db, "closed")
    still_open = _maint_incident(db, "open")
    anomaly = emit_event(db, event_type=EventType.PUBLIC_SURFACE_ANOMALY, payload={"failing": [{"name": "ui_root", "failure": "timeout", "final_status": None}]},
                         idempotency_key=f"surface:{uuid.uuid4()}")
    surface = company_mod.open_incident(db, reason="public surface unavailable: ui_root", source="public_surface", evidence={"anomaly_event_id": str(anomaly.id)})
    db.commit()

    assert tickets.board_decide(db, _board(settings), utcnow()) == 0
    db.commit()
    note = _row(db, pid)["board_note"]  # the owner sees it on the plan the same day; nothing is auto-lifted
    assert "incident freeze open (4)" in note and "2 outlived their recovered incident (lift them)" in note and str(recovered.id)[:8] in note
    assert set(company_mod.outlived_freezes(db)) == {str(recovered.id), str(closed.id)}

    operator = types.SimpleNamespace(id=None)
    calls = []

    def fake_api(method, url, token, body=None, timeout=30.0):
        calls.append((method, url, body))
        fid = uuid.UUID(url.rstrip("/").split("/")[-2])
        s = SessionLocal()
        try:
            return 200, company_mod.incident_view(company_mod.lift_incident(s, fid, operator=operator, reason=body["reason"]))
        finally:
            s.close()

    monkeypatch.setattr(p5, "api", fake_api)
    out, conn = p5.Out(), engine.raw_connection()
    try:
        p5.step_freezes(out, "http://registry", "tok", conn)
        assert out.failed == []
        assert sorted(c[1].split("/")[-2] for c in calls) == sorted([str(recovered.id), str(closed.id)])
        assert all(c[2] == {"reason": p5.FREEZE_LIFT_REASON} for c in calls)
        calls.clear()
        p5.step_freezes(out, "http://registry", "tok", conn)  # idempotent: nothing left to lift
        assert calls == [] and out.failed == []
    finally:
        conn.close()
    db.expire_all()
    open_ids = {str(i) for (i,) in db.execute(text("SELECT id FROM society_incident_freezes WHERE lifted_at IS NULL")).all()}
    assert open_ids == {str(still_open.id), str(surface.id)}, "a freeze without a recovered incident is never touched"
    printed = capsys.readouterr().out
    assert "lifted 2/2 through the operator API" in printed
    assert f"kept {str(surface.id)[:8]} public_surface" in printed and "'failure': 'timeout'" in printed and "surface recovered since: no" in printed
    assert p5.parse_plan("freezes:lift-recovered,autonomy:7") == [("freezes", ["lift-recovered"]), ("autonomy", ["7"])]
