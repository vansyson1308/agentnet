"""Growth supply: an active objective's growth key result that is off target becomes ONE proposed
ticket per week at the company cycle (backlog.kr_gap_items -> tickets.supply), with a lever packet.
Product work no longer depends on a model choosing to file it, and it is never RED by construction."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

from services.registry.app.society import backlog, tickets, work_packet
from services.registry.app.society.events import utcnow


def _activate(db, oid):
    db.execute(text("INSERT INTO society_objective_status (objective_id, status) VALUES (:o, 'active') ON CONFLICT (objective_id) DO UPDATE SET status = 'active'"),
               {"o": oid})
    db.commit()


def _tickets(db):
    return {r["proof"][0]: dict(r) for r in db.execute(text("SELECT * FROM society_tickets")).mappings()}


def test_only_active_objectives_off_target_growth_krs_are_items(db):
    assert backlog.kr_gap_items(db) == []  # every objective is only proposed in the charter
    _activate(db, "O2")
    _activate(db, "O3")  # bench pass@1 has its own supply: never a kr_gap item
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    items = backlog.kr_gap_items(db, now)
    assert {i["metric_id"] for i in items} == {"escrow_tasks_completed_7d", "a2a_agents_live_card"}
    assert all(i["key"].endswith(":2026-W42") and i["objective_id"] == "O2" and i["current"] is not None for i in items)


def test_a_growth_gap_becomes_one_weekly_ticket_with_a_probe_proof_and_a_lever_packet(db):
    _activate(db, "O2")
    made = tickets.supply(db, backlog.kr_gap_items(db), utcnow())
    db.commit()
    rows = _tickets(db)
    assert len(made) == 2 and set(rows) == {"probe:escrow_tasks_completed_7d", "probe:a2a_agents_live_card"}
    a2a = rows["probe:a2a_agents_live_card"]
    assert (a2a["objective_id"], a2a["metric_id"], a2a["department"], a2a["status"], a2a["source"]) == ("O2", "a2a_agents_live_card", "strategy_product", "proposed", "backlog")
    assert a2a["title"].startswith("Grow a2a_agents_live_card: 0.0 -> 10.0 (O2 Real marketplace)") and float(a2a["expected_effect"]) == 10.0
    assert not tickets.is_red(a2a) and tickets.proof_tests(a2a) == []
    assert tickets.supply(db, backlog.kr_gap_items(db), utcnow()) == [], "one open ticket per key result"
    packet = work_packet.for_ticket(db, a2a)
    assert packet["kind"] == "growth" and "services/dashboard/app/templates/network.html" in packet["levers"]
    # the plan ranks O2 (owner priority 2) growth above O3 bench work
    plan = tickets.build_plan(db, None, utcnow())
    assert {r["metric_id"] for r in plan["ranking"]} == {"escrow_tasks_completed_7d", "a2a_agents_live_card"}
    assert all(r["risk"] == "judged_from_diff" for r in plan["ranking"])


def test_a_met_key_result_is_no_item(db, monkeypatch):
    _activate(db, "O2")
    from services.registry.app.society import metrics

    real = metrics.read
    monkeypatch.setattr(metrics, "read", lambda db, m, now=None: 50.0 if m == "escrow_tasks_completed_7d" else real(db, m, now))
    assert [i["metric_id"] for i in backlog.kr_gap_items(db)] == ["a2a_agents_live_card"]
