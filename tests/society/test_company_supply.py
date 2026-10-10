"""Company mode: the backlog supplies tickets deterministically, so a plan never waits on a model."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import timedelta

from sqlalchemy import text

from services.registry.app.society import company as company_mod, tickets
from services.registry.app.society.events import utcnow
from services.registry.app.society.seed import seed_society

from .conftest import auth
from .test_backlog_loop import _incident, _report


class _Issues:
    def list_agent_ok_issues(self):
        return [{"number": 7, "title": "x", "updated_at": "2026-10-09T10:00:00Z", "labels": ["agent-ok", "O2"]},
                {"number": 8, "title": "y", "updated_at": "2026-10-09T10:00:00Z", "labels": ["agent-ok"]}]


def _settings(society_settings):
    return dataclasses.replace(society_settings, company_cycle_enabled=True)


def _tickets(db):
    return [dict(r) for r in db.execute(text("SELECT * FROM society_tickets ORDER BY title")).mappings()]


def test_every_owned_backlog_item_becomes_one_proposed_ticket_and_supply_is_idempotent(db, society_settings, monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "r-running")
    _report(db)  # dev: hard-task 1/3, flaky-task 2/3; holdout pass@1 0.0
    core = _incident(db)
    db.query(type(core)).filter_by(id=core.id).update({"desired_state_ref": "slo:core_journey"})
    _incident(db)  # a public-surface incident: no objective owns it
    db.commit()
    from services.registry.app.society import backlog

    items = backlog.collect(db, society_settings, _Issues())
    made = tickets.supply(db, items, utcnow())
    db.commit()
    rows = _tickets(db)
    assert len(made) == 4 and {r["status"] for r in rows} == {"proposed"} and {r["source"] for r in rows} == {"backlog"}
    by = {r["proof"][0]: r for r in rows}
    assert set(by) == {"bench:hard-task", "bench:flaky-task", "probe:core_journey_success_rate", "issue:7"}, "untagged issue #8 is not a ticket"
    bench = by["bench:hard-task"]
    assert (bench["objective_id"], bench["metric_id"], bench["department"], bench["direction"]) == ("O3", "bench_holdout_pass_at_1", "engineering", "up")
    assert float(bench["expected_effect"]) == round(0.863 / 2, 4), "the KR gap (target - current holdout 0.0) shared by its 2 items"
    assert (by["probe:core_journey_success_rate"]["objective_id"], by["probe:core_journey_success_rate"]["department"]) == ("O1", "sre_release")
    assert by["issue:7"]["objective_id"] == "O2" and by["issue:7"]["proposal_id"] is not None
    assert tickets.supply(db, backlog.collect(db, society_settings, _Issues()), utcnow()) == [], "idempotent per backlog key and subject"
    assert tickets.proof_tests({"proof": ["bench:x", "probe:y", "issue:7", "tests/a.py::t"]}) == ["tests/a.py::t"]


def test_a_cycle_with_an_active_objective_settles_into_a_non_empty_plan(db, society_settings, monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "r-running")
    seed_society(db)
    _report(db)
    db.execute(text("INSERT INTO society_objective_status (objective_id, status) VALUES ('O3', 'active')"))
    db.commit()
    cycle = company_mod.start_cycle(db, _settings(society_settings), trigger="operator")
    assert cycle.evidence["tickets_supplied"] == 2
    assert company_mod.settle_cycles(db, now=utcnow() + timedelta(hours=1)) == 1
    db.refresh(cycle)
    plan = db.execute(text("SELECT * FROM society_daily_plans WHERE id = :i"), {"i": cycle.outcome_detail["plan_id"]}).mappings().one()
    assert plan["status"] == "awaiting_owner" and len(plan["ticket_ids"]) == 2 and {r["department"] for r in plan["ranking"]} == {"engineering"}


def test_an_empty_plan_says_why_on_the_operator_view(db, society_settings, monkeypatch, api_client, user_token):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "r-running")
    seed_society(db)
    _report(db)
    company_mod.start_cycle(db, _settings(society_settings), trigger="operator")
    company_mod.settle_cycles(db, now=utcnow() + timedelta(hours=1))
    _, op = user_token("operator")
    body = api_client.get("/v1/society/company/plans", headers=auth(op)).json()
    assert body["plans"] == [] and body["empty_reason"].startswith("no active objective")


def test_a_model_may_only_add_two_tickets_a_day_per_department_and_must_cite_evidence(db):
    now = utcnow()
    assert "must cite evidence" in tickets.model_ticket_refusal(db, "scout", False, now)
    assert tickets.model_ticket_refusal(db, "scout", True, now) is None
    for i in range(2):
        db.execute(text("INSERT INTO society_tickets (id, title, objective_id, metric_id, expected_effect, direction, source, department) "
                        "VALUES (:i, 't', 'O3', 'bench_holdout_pass_at_1', 0.01, 'up', 'scout', 'strategy_product')"), {"i": uuid.uuid4()})
    db.commit()
    assert "already filed 2 ticket(s) today" in tickets.model_ticket_refusal(db, "scout", True, now)
    assert tickets.model_ticket_refusal(db, "qa", True, now) is None, "another department's quota is its own"
