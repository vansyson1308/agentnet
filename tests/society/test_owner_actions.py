"""Revenue first: in-repo revenue plays become O4 tickets; outside actions go to the owner's queue;
the owner's daily report is one deterministic page (scripted model only, no live claim).

Money invariant: nothing here touches wallets or transactions -- the queue and the report only READ
(revenue comes from goals.scorecard) and an owner action is a row an operator closes by hand.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid

from sqlalchemy import text

from services.registry.app.models import AgentIntent
from services.registry.app.society import backlog, company as company_mod, daily_report, owner_actions, tickets, work_packet
from services.registry.app.society.cognition import FakeModel
from services.registry.app.society.events import EventType, emit_event, utcnow
from services.registry.app.society.worker import SocietyWorker

from .conftest import auth
from .test_company_tickets import _activate, company as company  # noqa: F401 -- fixture
from .test_north_star import north_star_on  # noqa: F401 -- fixture


def _wallet_state(db):
    return db.execute(text("SELECT COUNT(*), COALESCE(SUM(balance_usdc), 0), COALESCE(SUM(balance_credits), 0) FROM wallets")).one(), \
        db.execute(text("SELECT COUNT(*) FROM transactions")).scalar()


def test_each_revenue_play_is_its_own_o4_ticket_with_the_plays_levers_first(db, company, north_star_on):
    _activate(db, "O4")
    items = [i for i in backlog.kr_gap_items(db) if i["metric_id"] in work_packet.REVENUE_PLAYS]
    assert {i["play"] for i in items} == {p["id"] for plays in work_packet.REVENUE_PLAYS.values() for p in plays}
    made = tickets.supply(db, items, utcnow())
    db.commit()
    assert len(made) == len(items) and tickets.supply(db, items, utcnow()) == [], "one ticket per play per week"
    for tid in made:
        t = tickets._ticket_ref(db, tid)
        assert t["objective_id"] == "O4" and t["proof"][0] == f"probe:{t['metric_id']}" and t["proof"][1].startswith("play:")
        play = next(p for p in work_packet.REVENUE_PLAYS[t["metric_id"]] if f"play:{p['id']}" == t["proof"][1])
        packet = work_packet.for_ticket(db, t)
        assert packet["play"]["id"] == play["id"] and packet["levers"][:len(play["levers"])] == play["levers"]
        assert play["title"] in t["title"]
        assert tickets.proof_tests(t) == [], "a play is not a pytest target"


def test_the_cycle_fills_the_owner_queue_once_for_active_north_star_objectives(db, company, north_star_on):
    settings = dataclasses.replace(company[1], north_star_enabled=True)
    before = _wallet_state(db)
    _activate(db, "O4")
    cycle = company_mod.start_cycle(db, settings, trigger="operator", now=utcnow())
    assert cycle.evidence["owner_actions_supplied"] == len(owner_actions.CATALOG["O4"])
    assert owner_actions.supply_catalog(db) == 0, "idempotent per key"
    _activate(db, "O5")
    assert owner_actions.supply_catalog(db) == len(owner_actions.CATALOG["O5"])
    db.commit()
    assert {a["objective_id"] for a in owner_actions.listing(db)} == {"O4", "O5"}
    assert _wallet_state(db) == before, "the queue never touches money"


def test_the_queue_is_empty_without_the_north_star(db, company):
    _, settings = company
    _activate(db, "O4")
    cycle = company_mod.start_cycle(db, settings, trigger="operator", now=utcnow())
    assert "owner_actions_supplied" not in cycle.evidence and owner_actions.open_count(db) == 0


def _propose(title, metric="revenue_usd_30d"):
    return {"type": "PROPOSE_OWNER_ACTION", "payload": {"title": title, "reason": "a public post in the owner's name",
                                                         "expected_effect": "visitors that become paying callers",
                                                         "metric_id": metric}}


def test_a_scout_queues_outside_actions_never_does_them_capped_and_deduplicated(db, SessionLocal, company):
    report, settings = company
    _activate(db, "O4")
    for title in ("Post a paid-agent demo on X", "Email ten agent developers"):  # two already queued today
        owner_actions.propose(db, agent_id=report.agents["governor"], title=title, reason="a public post in the owner's name",
                              expected_effect="paying callers", metric_id="revenue_usd_30d", now=utcnow())
    db.commit()
    intents = [_propose("post a PAID-agent demo   on x"),  # same title -> duplicate, not a new item
               _propose("List AgentNet on an outside directory", metric="verified_signups_7d"),  # an O5 KR; O5 is not active
               _propose("Write a Reddit launch thread"), _propose("Sponsor a newsletter issue")]  # the 3rd fills the day

    def decide(ctx):
        return {"decision_summary": "outside actions for the owner", "intents": intents, "sleep_for_seconds": 1}

    emit_event(db, event_type=EventType.SOCIETY_HEARTBEAT, payload={"bucket": 1}, actor_type="system", idempotency_key=f"hb:{uuid.uuid4()}")
    db.commit()
    worker = SocietyWorker(SessionLocal, settings=settings, model=FakeModel(decide), worker_id="w-owner", telemetry_enabled=False)
    worker.routing = {EventType.SOCIETY_HEARTBEAT: ["scout"]}
    asyncio.run(worker.run_until_idle(max_cycles=5))
    db.expire_all()
    rows = db.query(AgentIntent).filter(AgentIntent.intent_type == "PROPOSE_OWNER_ACTION").order_by(AgentIntent.seq).all()
    assert [r.execution_status.value for r in rows] == ["executed", "failed", "executed", "failed"]
    assert rows[0].result["result"] == {"duplicate": True} and "not a key result of an active objective" in rows[1].error
    assert f"at most {owner_actions.AGENT_ITEMS_PER_DAY}" in rows[3].error
    queued = owner_actions.listing(db)
    assert len(queued) == owner_actions.AGENT_ITEMS_PER_DAY and all(a["source"] == "agent" for a in queued)
    assert queued[0]["title"] == "Write a Reddit launch thread" and queued[0]["proposed_by_agent_id"] == str(report.agents["scout"])
    assert db.execute(text("SELECT COUNT(*) FROM society_events WHERE event_type = :t"), {"t": EventType.OWNER_ACTION_PROPOSED}).scalar() == 1


def test_only_an_operator_reads_and_closes_owner_actions(db, company, api_client, user_token, north_star_on):
    _activate(db, "O4")
    owner_actions.supply_catalog(db)
    db.commit()
    _, plain = user_token(None)
    _, op = user_token("operator")
    assert api_client.get("/v1/society/company/owner-actions", headers=auth(plain)).status_code == 403
    assert api_client.get("/v1/society/company/report", headers=auth(plain)).status_code == 403
    items = api_client.get("/v1/society/company/owner-actions", headers=auth(op)).json()["owner_actions"]
    aid = items[0]["id"]
    assert api_client.post(f"/v1/society/company/owner-actions/{aid}/done", headers=auth(plain)).status_code == 403
    r = api_client.post(f"/v1/society/company/owner-actions/{aid}/done", headers=auth(op), json={"note": "account opened"})
    assert r.status_code == 200 and r.json()["status"] == "done" and r.json()["note"] == "account opened"
    assert api_client.post(f"/v1/society/company/owner-actions/{aid}/dismissed", headers=auth(op)).status_code == 404, "closed once"
    assert api_client.post(f"/v1/society/company/owner-actions/{aid}/approve", headers=auth(op)).status_code == 422
    done = api_client.get("/v1/society/company/owner-actions?state=done", headers=auth(op)).json()["owner_actions"]
    assert [a["id"] for a in done] == [aid]


def test_the_daily_report_names_spend_builds_reviews_revenue_and_the_owner_queue(db, company, api_client, user_token, north_star_on):
    report, _ = company
    _activate(db, "O4")
    owner_actions.supply_catalog(db)
    seed = emit_event(db, event_type=EventType.SOCIETY_HEARTBEAT_IDLE, payload={}, actor_type="system", idempotency_key=f"seed:{uuid.uuid4()}")
    for role, cost in (("builder", 0.60), ("architect", 0.10)):
        db.execute(text("INSERT INTO agent_runs (id, agent_id, event_id, correlation_id, role, status, cost_usd, completed_at) "
                        "VALUES (:i, :a, :e, :e, :r, 'completed', :c, NOW())"), {"i": uuid.uuid4(), "a": report.agents[role], "e": seed.id, "r": role, "c": cost})
    db.commit()
    rep = daily_report.build(db)
    d = rep["data"]
    assert d["spend_usd_by_role"] == {"builder": 0.6, "architect": 0.1} and d["spend_usd_total"] == 0.7
    assert d["builds"] == {"attempted": 0, "delivered": 0} and d["owner_actions_open"] == len(owner_actions.CATALOG["O4"])
    assert "builder $0.60" in rep["text"] and "North star: $0.00 / $1,000,000" in rep["text"]
    assert all(a["title"] in rep["text"] for a in owner_actions.CATALOG["O4"])
    _, op = user_token("operator")
    assert api_client.get("/v1/society/company/report", headers=auth(op)).json()["data"]["spend_usd_total"] == 0.7
    summary = api_client.get("/v1/society/company/plans", headers=auth(op)).json()["summary_text"]
    assert summary.startswith("Last 24h:") and "Owner actions waiting: 2" in summary
