"""The Owner Console as a live conversation (owner_thread.py, owner 2026-10-29: "a CEO speaks and the
team answers in minutes"). Owner events are PRIORITY (dispatched and claimed first, no cooldown, no
heartbeat precheck, a separate owner reserve inside the global cap); a directive starts ONE cycle and
plan at once; the Governor answers each owner event at most once (RESPOND_TO_OWNER), and only in the
story that event opened. Deterministic plumbing: the model still only proposes typed intents and
nothing here grants anything."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import pathlib
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from services.registry.app.models import Agent, AgentCapabilityGrant, AgentIntent, AgentRun, AgentRunStatus, SocietyEvent
from services.registry.app.society import directives, owner_thread, promotion_queue, tickets
from services.registry.app.society.cognition import SYSTEM_PROMPT, FakeModel
from services.registry.app.society.events import OWNER_EVENT_TYPES, EventType, emit_event, utcnow
from services.registry.app.society.intents import RESPOND_LIMIT, IntentType
from services.registry.app.society.policy import check_run_budget
from services.registry.app.society.roles import DEFAULT_ROLES
from services.registry.app.society.runs import claim_next_run, dispatch_pending_events
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker

from .conftest import auth
from .test_company_tickets import _activate, company as company  # noqa: F401 -- fixture
from .test_north_star import _ticket

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _owner_event(db, entry):
    return db.query(SocietyEvent).filter(SocietyEvent.idempotency_key == f"owner:{entry['id']}").one()


def _respond(reply="Understood: the Scout files a ticket for it now and I rank it into a plan.", steps=("Scout files the ticket", "rank it into a plan"), ids=()):
    return {"type": "RESPOND_TO_OWNER", "payload": {"reply": reply, "plan_steps": list(steps), "ticket_ids": [str(i) for i in ids], "owner_actions_needed": []}}


# ── priority: dispatched and claimed first ─────────────────────────────


def test_owner_events_are_dispatched_and_claimed_before_older_work(db, society_settings):
    seed_society(db)
    for i in range(3):
        emit_event(db, event_type="t.busy", payload={"i": i}, idempotency_key=f"busy-{i}")
    db.commit()
    entry = owner_thread.record(db, kind="message", text_="what is the company doing about revenue?", user_id=None)
    db.commit()
    routing = {"t.busy": ["scout"], EventType.OWNER_MESSAGE: ["governor"]}
    dispatch_pending_events(db, settings=dataclasses.replace(society_settings, dispatch_batch_size=1), routing=routing)
    ev = _owner_event(db, entry)
    assert _ev(ev.status) != "pending", "a batch of one takes the owner's event, not the older ones"
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == "t.busy", SocietyEvent.status == "pending").count() == 3
    dispatch_pending_events(db, settings=society_settings, routing=routing)
    assert claim_next_run(db, worker_id="lane", lease_seconds=60, owner_only=True).event_id == ev.id
    assert claim_next_run(db, worker_id="lane", lease_seconds=60, owner_only=True) is None, "the owner lane claims only owner runs"
    assert claim_next_run(db, worker_id="main", lease_seconds=60).event_id != ev.id


def test_the_main_claim_takes_an_owner_run_before_older_queued_runs(db, society_settings):
    seed_society(db)
    emit_event(db, event_type="t.busy", payload={}, idempotency_key="busy-old")
    db.commit()
    dispatch_pending_events(db, settings=society_settings, routing={"t.busy": ["scout"]})
    entry = owner_thread.record(db, kind="task", text_="ship the O4 quote page", user_id=None)
    db.commit()
    dispatch_pending_events(db, settings=society_settings, routing={EventType.OWNER_TASK_CREATED: ["governor"]})
    assert claim_next_run(db, worker_id="main", lease_seconds=60).event_id == _owner_event(db, entry).id


# ── budget: the owner reserve, no cooldown ─────────────────────────────


@pytest.fixture
def fleet(db):
    report = seed_society(db)
    agents = {r: db.query(Agent).filter(Agent.id == a).one() for r, a in report.agents.items()}
    grants = {r: db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == a).one() for r, a in report.agents.items()}
    for g in grants.values():
        g.max_runs_per_hour, g.wake_cooldown_seconds = 100, 0
    db.commit()
    return agents, grants


def _queued(db, settings, role, event_type, **owner):
    if event_type in OWNER_EVENT_TYPES:
        entry = owner_thread.record(db, kind={v: k for k, v in owner_thread.KINDS.items()}[event_type], text_="owner asks", user_id=None)
        ev = _owner_event(db, entry)
    else:
        ev = emit_event(db, event_type=event_type, payload={}, idempotency_key=f"t-{uuid.uuid4()}")
    db.commit()
    dispatch_pending_events(db, settings=settings, routing={event_type: [role]})
    return db.query(AgentRun).filter(AgentRun.event_id == ev.id).one()


def _spent(db, run, usd, *, ago=timedelta(minutes=30)):
    run.status, run.cost_usd, run.started_at, run.completed_at = AgentRunStatus.COMPLETED, Decimal(usd), utcnow() - ago, utcnow() - ago
    db.commit()


def test_the_owner_reserve_is_held_back_from_other_work_and_spent_only_by_owner_runs(db, fleet, society_settings):
    agents, grants = fleet
    settings = dataclasses.replace(society_settings, daily_model_budget_usd=Decimal("20"), owner_reserve_usd=Decimal("5"), max_correlation_cost_usd=Decimal("50"))
    grants["governor"].daily_model_budget_usd = Decimal("100")
    grants["scout"].daily_model_budget_usd = Decimal("0.01")
    db.commit()
    _spent(db, _queued(db, settings, "governor", "t.work"), "15.5")  # ordinary work: 15.5 of 20; 5 of it is the owner's
    plain = _queued(db, settings, "governor", "t.work")
    v = check_run_budget(db, agent=agents["governor"], grant=grants["governor"], settings=settings, run=plain)
    assert not v.ok and "owner reserve" in v.reason, v.reason
    owner_run = _queued(db, settings, "scout", EventType.OWNER_MESSAGE)
    assert check_run_budget(db, agent=agents["scout"], grant=grants["scout"], settings=settings, run=owner_run, owner=True).ok, \
        "the owner's run spends the reserve, not the role's (exhausted) daily budget"
    assert not check_run_budget(db, agent=agents["scout"], grant=grants["scout"], settings=settings, run=owner_run).ok
    _spent(db, owner_run, "4.4")
    _spent(db, _queued(db, settings, "governor", EventType.OWNER_TASK_CREATED), "0.6")  # the reserve is spent: 5.0 of 5
    roomy = dataclasses.replace(settings, daily_model_budget_usd=Decimal("100"))
    later = _queued(db, roomy, "governor", EventType.OWNER_MESSAGE)
    v = check_run_budget(db, agent=agents["governor"], grant=grants["governor"], settings=roomy, run=later, owner=True)
    assert not v.ok and "owner reserve exhausted" in v.reason
    assert check_run_budget(db, agent=agents["governor"], grant=grants["governor"], settings=roomy, run=plain).ok, "a spent reserve holds nothing back"
    # 20.5 >= 20: the global cap binds every run, the owner's too
    v = check_run_budget(db, agent=agents["governor"], grant=grants["governor"], settings=dataclasses.replace(settings, owner_reserve_usd=Decimal("10")), run=later, owner=True)
    assert not v.ok and "daily model budget exhausted" in v.reason


def test_without_a_reserve_owner_runs_keep_the_role_budget_but_skip_the_cooldown(db, fleet, society_settings):
    agents, grants = fleet
    assert society_settings.owner_reserve_usd == 0, "off by default"
    g = grants["governor"]
    g.wake_cooldown_seconds = 3600
    db.commit()
    _spent(db, _queued(db, society_settings, "governor", "t.work"), "0.001", ago=timedelta(seconds=10))
    plain = _queued(db, society_settings, "governor", "t.work")
    v = check_run_budget(db, agent=agents["governor"], grant=g, settings=society_settings, run=plain)
    assert not v.ok and "cooldown" in v.reason and v.retry_after_seconds
    owner_run = _queued(db, society_settings, "governor", EventType.OWNER_MESSAGE)
    assert check_run_budget(db, agent=agents["governor"], grant=g, settings=society_settings, run=owner_run, owner=True).ok
    g.daily_model_budget_usd = Decimal("0.001")
    db.commit()
    v = check_run_budget(db, agent=agents["governor"], grant=g, settings=society_settings, run=owner_run, owner=True)
    assert not v.ok and "agent daily model budget" in v.reason


def test_the_reserve_must_fit_inside_the_global_cap(monkeypatch):
    from services.registry.app.society.config import SocietyConfigError, SocietySettings

    monkeypatch.setenv("SOCIETY_DAILY_MODEL_BUDGET", "10")
    monkeypatch.setenv("SOCIETY_OWNER_RESERVE_USD", "11")
    with pytest.raises(SocietyConfigError, match="SOCIETY_OWNER_RESERVE_USD"):
        SocietySettings()
    monkeypatch.setenv("SOCIETY_OWNER_RESERVE_USD", "5")
    assert SocietySettings().owner_reserve_usd == 5


# ── the Governor answers, once ──────────────────────────────────────────


def _worker(SessionLocal, settings, decide, wid="w-owner"):
    return SocietyWorker(SessionLocal, settings=settings, model=FakeModel(decide), worker_id=wid, telemetry_enabled=False)


def test_the_governor_answers_an_owner_message_once_even_past_the_heartbeat_precheck(db, SessionLocal, society_settings, grants_with_no_cooldown):
    seed_society(db)
    grants_with_no_cooldown()
    settings = dataclasses.replace(society_settings, heartbeat_precheck_enabled=True)
    entry = owner_thread.record(db, kind="message", text_="Tìm khách hàng trả tiền đầu tiên tuần này", user_id=None)
    db.commit()
    seen = {}

    def decide(ctx):
        if ctx.owner_request:
            seen[ctx.role] = ctx.owner_request
        if ctx.role == "governor" and ctx.owner_request and not ctx.owner_request["answered"]:
            return {"decision_summary": "answer the owner", "intents": [_respond(), _respond(reply="A second answer to the same owner message.")], "sleep_for_seconds": 60}
        return {"decision_summary": "nothing to do", "intents": [], "sleep_for_seconds": 60}

    asyncio.run(_worker(SessionLocal, settings, decide).run_until_idle(max_cycles=4))
    db.expire_all()
    assert set(seen) == {"governor", "scout"}, "the chair and the Scout are woken"
    assert seen["governor"]["text"] == "Tìm khách hàng trả tiền đầu tiên tuần này" and seen["governor"]["label"].startswith("OWNER INPUT")
    rows = db.query(AgentIntent).filter(AgentIntent.intent_type == "RESPOND_TO_OWNER").order_by(AgentIntent.created_at).all()
    assert [_ev(r.execution_status) for r in rows] == ["executed", "failed"] and "already answered" in rows[1].error
    replies = db.execute(text("SELECT text, payload, reply_to FROM society_owner_thread WHERE author = 'company'")).mappings().all()
    assert len(replies) == 1 and str(replies[0]["reply_to"]) == entry["id"] and replies[0]["payload"]["plan_steps"] == ["Scout files the ticket", "rank it into a plan"]
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.OWNER_REPLIED).count() == 1
    view = owner_thread.view(db)
    assert view["entries"][0]["status"] == "answered" and view["entries"][0]["reply"]["text"].startswith("Understood") and view["working"] is False
    slo = owner_thread.slo(db)
    assert slo["reply"]["n"] == 1 and slo["reply"]["ok"] is True and slo["first_run_start"]["n"] == 1 and slo["first_run_start"]["ok"] is True
    with pytest.raises(IntegrityError):  # the database holds the line too: one reply per owner row
        db.execute(text("INSERT INTO society_owner_thread (id, author, kind, text, reply_to) VALUES (:i, 'company', 'reply', 'again', :r)"),
                   {"i": uuid.uuid4(), "r": uuid.UUID(entry["id"])})
    db.rollback()


def test_respond_to_owner_is_refused_outside_an_owner_story_and_only_the_governor_holds_it(db, SessionLocal, society_settings, grants_with_no_cooldown):
    assert [r for r, d in DEFAULT_ROLES.items() if IntentType.RESPOND_TO_OWNER.value in d.allowed_intents] == ["governor"]
    seed_society(db)
    grants_with_no_cooldown()
    emit_event(db, event_type="t.plain", payload={}, idempotency_key="t-plain")
    db.commit()
    worker = _worker(SessionLocal, society_settings, lambda ctx: {"decision_summary": "x", "intents": [_respond()], "sleep_for_seconds": 60})
    worker.routing = {"t.plain": ["governor", "scout"]}
    asyncio.run(worker.run_until_idle(max_cycles=3))
    db.expire_all()
    rows = {r.agent_id: r for r in db.query(AgentIntent).filter(AgentIntent.intent_type == "RESPOND_TO_OWNER").all()}
    statuses = sorted((_ev(r.execution_status), _ev(r.policy_decision)) for r in rows.values())
    assert len(rows) == 2 and not db.execute(text("SELECT 1 FROM society_owner_thread")).first()
    assert ("failed", "allow") in statuses and any(p == "deny" for _, p in statuses), statuses
    assert any("this story has none" in (r.error or "") for r in rows.values())


def test_the_owner_thread_is_append_only_and_redacted(db):
    secret = "sk-ant-" + "api03-" + "A" * 40  # a key SHAPE (not a credential)
    entry = owner_thread.record(db, kind="message", text_=f"use this {secret} please", user_id=None)
    db.commit()
    stored = db.execute(text("SELECT text FROM society_owner_thread WHERE id = :i"), {"i": uuid.UUID(entry["id"])}).scalar()
    assert secret not in stored and "REDACTED" in stored.upper()
    for sql in ("UPDATE society_owner_thread SET text = 'rewritten'", "DELETE FROM society_owner_thread"):
        with pytest.raises(DBAPIError, match="append-only"):
            db.execute(text(sql))
        db.rollback()


# ── a directive starts work now ─────────────────────────────────────────


def test_a_directive_starts_one_cycle_and_a_plan_marked_from_it(db, company):
    report, settings = company
    settings = dataclasses.replace(settings, runtime_enabled=True)
    _activate(db, "O5")
    tid = _ticket(db, report, "O5", "verified_signups_7d", 15)
    d = directives.set_directive(db, text_="signups first", objective_ids=["O5"], user_id=None)
    out = owner_thread.directive_set(db, settings, d, user_id=None)
    assert out["cycle_id"] and out["plan"]["id"] and [r["ticket_id"] for r in out["plan"]["ranking"]] == [tid]
    plan = db.execute(text("SELECT directive_id, owner_entry_id, cycle_id FROM society_daily_plans WHERE id = :i"), {"i": uuid.UUID(out["plan"]["id"])}).mappings().one()
    assert (str(plan["directive_id"]), str(plan["owner_entry_id"]), str(plan["cycle_id"])) == (d["id"], out["id"], out["cycle_id"])
    ev = _owner_event(db, out)
    assert ev.event_type == EventType.OWNER_DIRECTIVE_SET and ev.payload["directive_id"] == d["id"] and ev.payload["cycle_id"] == out["cycle_id"]
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == "company.cycle").count() == 0, "the owner's event IS the cycle's event"
    again = owner_thread.directive_set(db, settings, d, user_id=None)
    assert again["cycle_id"] is None and "already" in again["note"]
    assert db.execute(text("SELECT COUNT(*) FROM society_company_cycles WHERE directive_id = :d"), {"d": uuid.UUID(d["id"])}).scalar() == 1, "one cycle per directive"
    assert _owner_event(db, again).event_type == EventType.OWNER_DIRECTIVE_SET, "the owner still gets an answer"
    slo = owner_thread.slo(db)
    assert slo["directive_to_plan"]["n"] == 1 and slo["directive_to_plan"]["ok"] is True


def test_with_company_mode_off_a_directive_still_wakes_the_company(db, society_settings):
    d = directives.set_directive(db, text_="signups first", objective_ids=[], user_id=None)
    out = owner_thread.directive_set(db, dataclasses.replace(society_settings, company_cycle_enabled=False), d, user_id=None)
    assert out["cycle_id"] is None and _owner_event(db, out).event_type == EventType.OWNER_DIRECTIVE_SET
    assert db.execute(text("SELECT COUNT(*) FROM society_company_cycles")).scalar() == 0


def test_the_owner_lane_answers_and_plans_what_the_scout_files_for_the_owner(db, SessionLocal, company):
    report, settings = company
    settings = dataclasses.replace(settings, runtime_enabled=True)
    _activate(db, "O5")
    for _ in range(tickets.MODEL_TICKETS_PER_DEPARTMENT):  # the Scout's department cap is spent today
        _ticket(db, report, "O5", "verified_signups_7d", 1)
    db.execute(text("UPDATE society_tickets SET status = 'closed'"))
    db.commit()
    emit_event(db, event_type="t.busy", payload={}, idempotency_key="t-busy")  # older, non-owner work waits
    db.commit()
    entry = owner_thread.record(db, kind="message", text_="double verified signups this week", user_id=None)
    db.commit()
    filed = {"ticket": {"objective_id": "O5", "metric_id": "verified_signups_7d", "expected_effect": 10, "direction": "up", "proof": ["probe:verified_signups_7d"]},
             "evidence": {"signal": "owner_request", "actionable_reason": "the owner asked for it on the Owner Console"}}

    def decide(ctx):
        if ctx.role == "scout" and ctx.owner_request:
            return {"decision_summary": "file it", "intents": [{"type": "CREATE_IMPROVEMENT", "payload": {
                "title": "Signup funnel fix", "problem": "verified signups are flat", "proposed_change": "fix the verify step", **filed}}], "sleep_for_seconds": 60}
        if ctx.role == "governor" and ctx.owner_request and not ctx.owner_request["answered"]:
            return {"decision_summary": "answer", "intents": [_respond()], "sleep_for_seconds": 60}
        return {"decision_summary": "nothing", "intents": [], "sleep_for_seconds": 60}

    worker = _worker(SessionLocal, settings, decide)
    worker.routing = {**worker.routing, "t.busy": ["builder"]}
    asyncio.run(worker.owner_lane_once())
    db.expire_all()
    busy = db.query(SocietyEvent).filter(SocietyEvent.event_type == "t.busy").one()
    assert _ev(busy.status) == "pending", "the owner lane serves only the owner's events"
    view = owner_thread.view(db)["entries"][0]
    assert view["id"] == entry["id"] and view["status"] == "answered"
    assert [t["title"] for t in view["tickets"]] == ["Signup funnel fix"], "filed for the owner past the department's daily cap"
    assert len(view["plans"]) == 1 and view["tickets"][0]["planned"], "ranked into a plan now, not at the next scheduled cycle"
    assert tickets.model_ticket_refusal(db, "scout", True, utcnow()) is not None, "outside an owner story the cap still binds"
    assert "per owner event" in tickets.model_ticket_refusal(db, "scout", True, utcnow(), owner_story_tickets=tickets.OWNER_STORY_TICKETS)


# ── the API: operators only ─────────────────────────────────────────────


def test_only_an_operator_talks_to_the_company(db, api_client, user_token):
    _, plain = user_token(None)
    _, op = user_token("operator")
    assert api_client.post("/v1/society/company/owner-messages", json={"text": "hi"}).status_code == 401
    assert api_client.post("/v1/society/company/owner-messages", headers=auth(plain), json={"text": "hi"}).status_code == 403
    assert api_client.get("/v1/society/company/owner-thread", headers=auth(plain)).status_code == 403
    assert api_client.post("/v1/society/company/owner-messages", headers=auth(op), json={"text": ""}).status_code == 422
    r = api_client.post("/v1/society/company/owner-messages", headers=auth(op), json={"text": "what blocks the first paid task?"})
    assert r.status_code == 201 and r.json()["status"] == "received"
    view = api_client.get("/v1/society/company/owner-thread", headers=auth(op)).json()
    assert view["entries"][0]["text"] == "what blocks the first paid task?" and view["entries"][0]["status"] == "received" and view["working"] is True
    assert set(view["slo"]) == {"window_days", "first_run_start", "reply", "directive_to_plan"}
    routes = (ROOT / "services/registry/app/api/routes/society.py").read_text(encoding="utf-8")
    for path in ('@router.post("/company/owner-messages"', '@router.get("/company/owner-thread"'):
        assert "Depends(require_operator)" in routes[routes.index(path):].split("@router.", 2)[1]
    public = [p for p in (ROOT / "services/registry/app/api/routes").rglob("*.py") if "owner_thread" in p.read_text(encoding="utf-8")]
    assert [p.name for p in public] == ["society.py"], "the conversation is shown only through the operator routes"


def test_the_prompt_states_the_reply_limits():
    assert RESPOND_LIMIT in " ".join(SYSTEM_PROMPT.split())


# ── READY reasons come from the worker, not the registry's env ──────────


def test_ready_reasons_use_the_workers_heartbeat_caps(db, society_settings):
    registry = dataclasses.replace(society_settings, promotion_provider="disabled", max_open_autonomous_prs=1, github_repository="")
    assert promotion_queue.worker_caps(db) is None
    assert promotion_queue.worker_view_caps(db, registry)["provider"] == "disabled"
    emit_event(db, event_type=EventType.SOCIETY_HEARTBEAT, payload={"bucket": 1, "change_budget": {
        "provider": "github", "max_promotions_per_day": 3, "max_open_autonomous_prs": 3, "auto_merge_enabled": False}}, idempotency_key="hb-1")
    db.commit()
    caps = promotion_queue.worker_view_caps(db, registry)
    assert (caps["provider"], caps["max_open_autonomous_prs"], caps["max_promotions_per_day"]) == ("github", 3, 3)


# ── the validator's owner:slo ──────────────────────────────────────────


def _p5():
    spec = importlib.util.spec_from_file_location("phase5_live_owner_slo", ROOT / "deploy/railway/phase5_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_validator_owner_slo_step(monkeypatch):
    p5 = _p5()
    assert p5.parse_plan("owner:slo") == [("owner", ["slo"])]
    with pytest.raises(ValueError):
        p5.parse_plan("owner:slow")
    slo = {"window_days": 7, "first_run_start": {"n": 3, "p95_s": 4.0, "target_s": 30, "late_unanswered": 0, "ok": True},
           "reply": {"n": 2, "p95_s": 150.0, "target_s": 120, "late_unanswered": 0, "ok": False},
           "directive_to_plan": {"n": 0, "p95_s": None, "target_s": 600, "late_unanswered": 0, "ok": None}}
    monkeypatch.setattr(p5, "api", lambda method, url, token, body=None, timeout=30.0: (200, {"entries": [], "working": False, "slo": slo}))
    checks, infos, blobs = [], [], {}

    class Out:
        def check(self, group, code, ok, detail=""):
            checks.append((code, bool(ok)))
            return bool(ok)

        def info(self, group, detail):
            infos.append(detail)

        def json(self, group, key, obj):
            blobs[key] = obj

    p5.step_owner_slo(Out(), "https://api.test", "operator-jwt")
    assert checks == [("L00", True), ("L01", True), ("L02", False)] and infos and infos[0].startswith("L03 no data")
    assert blobs["slo"] == slo
