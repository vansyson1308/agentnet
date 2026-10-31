"""G1v -- the company must never run out of work (owner 2026-10-29, full throttle on staging).

At $80/day the company spent ~$7.7 in 3.5h: the Builder skipped 16 runs "idle: nothing to act on",
the newest plan was EMPTY, the directive produced 0 tickets in 6h and 3 READY candidates had no PR
(open PRs 0/5). The cause is SUPPLY: owner words did not become tickets. Now, deterministically:

* the Governor decomposes the owner's words into 1..5 tickets in the SAME owner story (only there),
  ranked into a plan within minutes; a story that started no work is re-woken ONCE
  (owner_event_without_work); SLO owner message -> ticket in a plan p95 < 10 min;
* tickets still waiting for a plan are backlog, not hypotheses holding the portfolio cap;
* a re-offered READY candidate the Governor did not request gets a recorded reason, and -- with the
  fallback on and capacity free -- the runtime's request through the Governor's own policy verdict,
  the same change budgets and the same controller;
* an approved ticket nobody is designing gets its Architect wake again (a wake, not an approval);
* the validator sets a role's runs/hour (`runs:<role>:<n>`), and a re-seed keeps it.

No new authority: grants (one: the Governor's CREATE_IMPROVEMENT, refused outside an owner story),
policy, budgets, the RED/merge law, files_allowed and auto-merge are unchanged."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import pathlib
import uuid
from datetime import timedelta

from sqlalchemy import text

from services.registry.app.models import AgentCapabilityGrant, AgentIntent, AgentRun, CodePromotion, ImprovementProposal, SocietyEvent
from services.registry.app.society import company as company_mod, owner_thread, promotion_queue, tickets, wake_precheck
from services.registry.app.society.cognition import SYSTEM_PROMPT, FakeModel
from services.registry.app.society.events import OWNER_EVENT_TYPES, EventType, emit_event, utcnow
from services.registry.app.society.roles import DEFAULT_ROLES
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker

from .test_company_tickets import _activate, company as company_fixture  # noqa: F401 -- fixture
from .test_promotion_queue import IDLE, _age, _ready
from .test_redelivery import _settings

ROOT = pathlib.Path(__file__).resolve().parents[2]
company = company_fixture


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _worker(SessionLocal, settings, decide, wid="w-g1v"):
    return SocietyWorker(SessionLocal, settings=settings, model=FakeModel(decide), worker_id=wid, telemetry_enabled=False)


def _ticket_intent(title, metric="verified_signups_7d", objective="O5"):
    return {"type": "CREATE_IMPROVEMENT", "payload": {
        "title": title, "problem": f"{title}: the owner asked for it", "proposed_change": f"do {title}",
        "ticket": {"objective_id": objective, "metric_id": metric, "expected_effect": 5, "direction": "up", "proof": [f"probe:{metric}"]},
        "evidence": {"signal": "owner_request", "actionable_reason": "the owner asked for it on the Owner Console"}}}


def _reply(steps, text_="Understood: three tickets carry it, ranked into a plan now."):
    return {"type": "RESPOND_TO_OWNER", "payload": {"reply": text_, "plan_steps": steps, "ticket_ids": [], "owner_actions_needed": []}}


def _intents(db):
    return [(i.intent_type, _ev(i.execution_status), i.error or i.policy_reason) for i in db.query(AgentIntent).order_by(AgentIntent.created_at, AgentIntent.seq).all()]


# ── 1. owner words -> tickets, in the same story ────────────────────────


def test_the_governor_turns_an_owner_message_into_tickets_planned_in_minutes(db, SessionLocal, company):
    report, settings = company
    settings = dataclasses.replace(settings, runtime_enabled=True, company_max_active_hypotheses=2)
    _activate(db, "O5")
    entry = owner_thread.record(db, kind="message", text_="upgrade the UI and bring traffic: onboarding, landing, signup", user_id=None)
    db.commit()
    titles = ["Landing page hero states the offer", "Signup form shows errors inline", "Onboarding checklist after signup"]

    def decide(ctx):
        if ctx.role == "governor" and ctx.owner_request and not ctx.owner_request["answered"]:
            steps = [{"step": f"ticket: {t}", "intent": i + 1} for i, t in enumerate(titles)] + [{"step": "design review after approval"}]
            return {"decision_summary": "decompose", "intents": [_ticket_intent(t) for t in titles] + [_reply(steps)], "sleep_for_seconds": 60}
        return {"decision_summary": "nothing", "intents": [], "sleep_for_seconds": 60}

    asyncio.run(_worker(SessionLocal, settings, decide).owner_lane_once())
    db.expire_all()
    rows = db.execute(text("SELECT title, source, status, plan_id FROM society_tickets ORDER BY created_at")).mappings().all()
    assert [r["title"] for r in rows] == titles, _intents(db)
    assert {r["source"] for r in rows} == {"owner_story"}, "an owner story's tickets say so (the funnel counts them by source)"
    assert {o for (o,) in db.execute(text("SELECT origin FROM society_tickets")).all()} == {"message"}, "and what the owner said: a message (or a directive)"
    assert all(r["status"] == "planned" and r["plan_id"] for r in rows), ("ranked into a plan in the same lane pass", [dict(r) for r in rows], settings.company_cycle_enabled)
    assert company_mod.active_hypothesis_count(db, settings) == 0, "a ticket waiting for its plan is backlog, not a hypothesis holding the cap"
    e = owner_thread.view(db)["entries"][0]
    assert e["id"] == entry["id"] and e["status"] == "answered"
    assert sorted(t["title"] for t in e["tickets"]) == sorted(titles)
    assert len(e["reply"]["ticket_ids"]) == 3, "the reply lists every ticket the story filed"
    assert [(s["state"], s.get("type"), bool(s.get("ticket_id"))) for s in e["reply"]["plan_steps"]] == [("started", "CREATE_IMPROVEMENT", True)] * 3 + [("next", None, False)]
    slo = owner_thread.slo(db)
    assert slo["message_to_plan"]["n"] == 1 and slo["message_to_plan"]["ok"] is True and slo["first_work"]["ok"] is True
    assert owner_thread.rewake_without_work(db, settings) == 0, "a story that started work is never re-woken"


def test_at_most_five_tickets_per_owner_event_and_the_governor_files_none_elsewhere(db, SessionLocal, company):
    report, settings = company
    settings = dataclasses.replace(settings, runtime_enabled=True)
    _activate(db, "O5")
    owner_thread.record(db, kind="message", text_="everything for signups", user_id=None)
    db.commit()

    def decide(ctx):
        if ctx.role == "governor" and ctx.owner_request and not ctx.owner_request["answered"]:
            return {"decision_summary": "too many", "intents": [_ticket_intent(f"Signup piece {i}") for i in range(6)], "sleep_for_seconds": 60}
        return {"decision_summary": "nothing", "intents": [], "sleep_for_seconds": 60}

    asyncio.run(_worker(SessionLocal, settings, decide).owner_lane_once())
    db.expire_all()
    done = [x for x in _intents(db) if x[0] == "CREATE_IMPROVEMENT"]
    assert [x[1] for x in done] == ["executed"] * 5 + ["failed"] and "at most 5 per owner event" in done[-1][2], done

    emit_event(db, event_type=EventType.SOCIETY_HEARTBEAT, payload={}, idempotency_key="t-hb-g1v")
    db.commit()

    def outside(ctx):
        if ctx.role == "governor" and not ctx.owner_request:
            return {"decision_summary": "file one myself", "intents": [_ticket_intent("Governor's own idea")], "sleep_for_seconds": 60}
        return {"decision_summary": "nothing", "intents": [], "sleep_for_seconds": 60}

    w = _worker(SessionLocal, settings, outside, wid="w-g1v-hb")
    w.routing = {EventType.SOCIETY_HEARTBEAT: ["governor"]}
    asyncio.run(w.run_until_idle(max_cycles=4))
    db.expire_all()
    last = db.query(AgentIntent).filter(AgentIntent.intent_type == "CREATE_IMPROVEMENT").order_by(AgentIntent.created_at.desc()).first()
    assert _ev(last.execution_status) == "failed" and "only in an owner story" in last.error
    assert db.query(ImprovementProposal).filter(ImprovementProposal.title == "Governor's own idea").count() == 0


def test_a_story_that_started_no_work_wakes_the_governor_once_more(db, SessionLocal, company):
    report, settings = company
    settings = dataclasses.replace(settings, runtime_enabled=True)
    _activate(db, "O5")
    entry = owner_thread.record(db, kind="message", text_="bring outside agents to the platform", user_id=None)
    db.commit()
    seen = []

    def decide(ctx):
        if ctx.role == "governor" and ctx.owner_request:
            seen.append(ctx.owner_request.get("rewake"))
            if not ctx.owner_request["answered"]:
                return {"decision_summary": "words only", "intents": [_reply(["think about it"], "Good idea, we will look into it.")], "sleep_for_seconds": 60}
            return {"decision_summary": "now work", "intents": [_ticket_intent("Directory listing for outside agents", "outside_agents_joined_7d")], "sleep_for_seconds": 60}
        return {"decision_summary": "nothing", "intents": [], "sleep_for_seconds": 60}

    w = _worker(SessionLocal, settings, decide)
    asyncio.run(w.owner_lane_once())  # the answer, no work -> recorded and re-woken at the end of the pass
    db.expire_all()
    (rw,) = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.OWNER_REWAKE).all()
    origin = db.query(SocietyEvent).filter(SocietyEvent.idempotency_key == f"owner:{entry['id']}").one()
    assert rw.payload["reason"] == owner_thread.WITHOUT_WORK and rw.correlation_id == origin.correlation_id, "the same story"
    assert EventType.OWNER_REWAKE in OWNER_EVENT_TYPES, "priority like the owner's own events"
    assert owner_thread.view(db)["entries"][0]["rewoken"] == owner_thread.WITHOUT_WORK
    asyncio.run(w.owner_lane_once())  # the Governor, re-woken, starts the work
    db.expire_all()
    assert seen[0] is None and seen[-1]["reason"] == owner_thread.WITHOUT_WORK
    assert db.execute(text("SELECT source FROM society_tickets")).scalars().all() == ["owner_story"]
    assert owner_thread.rewake_without_work(db, settings) == 0
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.OWNER_REWAKE).count() == 1, "once, never twice"


def test_a_story_still_without_work_after_the_rewake_is_not_rewoken_again(db, SessionLocal, company):
    report, settings = company
    settings = dataclasses.replace(settings, runtime_enabled=True)
    owner_thread.record(db, kind="message", text_="hello", user_id=None)
    owner_thread.record(db, kind="task", text_="an owner task is already a ticket", user_id=None)
    db.commit()

    def idle(ctx):
        return {"decision_summary": "nothing", "intents": [], "sleep_for_seconds": 60}

    w = _worker(SessionLocal, settings, idle)
    for _ in range(3):
        asyncio.run(w.owner_lane_once())
    db.expire_all()
    rewakes = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.OWNER_REWAKE).all()
    assert [r.payload["kind"] for r in rewakes] == ["message"], "one rewake, for the message only"


# ── 2. READY -> PR must actually happen ─────────────────────────────────


def _reoffer_and_idle_governor(db, SessionLocal, settings, report, title="O4 quote and accept"):
    cand = _ready(db, report, title)
    _age(db, cand)
    assert promotion_queue.retry_deferred(db, settings) == 1
    w = _worker(SessionLocal, settings, lambda ctx: IDLE, wid="w-g1v-reoffer")
    w.routing = {EventType.CODE_CANDIDATE_READY: ["governor"]}
    asyncio.run(w.run_until_idle(max_cycles=6))
    db.expire_all()
    return cand


def test_a_reoffer_the_governor_ignored_records_why_and_off_requests_nothing(db, SessionLocal, society_settings, monkeypatch, grants_with_no_cooldown):
    settings = _settings(monkeypatch, SOCIETY_PROMOTION_PROVIDER="fake", SOCIETY_MAX_PROMOTIONS_PER_DAY=6, SOCIETY_MAX_OPEN_AUTONOMOUS_PRS=5)
    assert settings.ready_fallback_enabled is False, "off by default"
    report = seed_society(db)
    grants_with_no_cooldown()
    cand = _reoffer_and_idle_governor(db, SessionLocal, settings, report)
    assert promotion_queue.settle_reoffers(db, settings) == 1
    (rec,) = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.READY_REOFFER_SETTLED).all()
    assert (rec.payload["reason"], rec.payload["fallback"], rec.subject_id) == ("governor_did_not_request", "off", cand.id)
    assert db.query(CodePromotion).count() == 0
    (b,) = promotion_queue.ready_backlog(db, settings)
    assert (b["reason"], b["detail"]) == ("not_requested", "governor_did_not_request: off"), "the READY list says why"
    assert promotion_queue.settle_reoffers(db, settings) == 0, "one record per re-offer"


def test_with_the_fallback_on_the_runtime_requests_it_through_the_governors_policy(db, SessionLocal, society_settings, monkeypatch, grants_with_no_cooldown):
    settings = _settings(monkeypatch, SOCIETY_PROMOTION_PROVIDER="fake", SOCIETY_MAX_PROMOTIONS_PER_DAY=6, SOCIETY_MAX_OPEN_AUTONOMOUS_PRS=5,
                         SOCIETY_READY_FALLBACK_ENABLED="true")
    report = seed_society(db)
    grants_with_no_cooldown()
    cand = _reoffer_and_idle_governor(db, SessionLocal, settings, report)
    assert promotion_queue.settle_reoffers(db, settings) == 1
    (promo,) = db.query(CodePromotion).all()
    assert promo.candidate_id == cand.id and _ev(promo.status) == "requested", "the controller takes it from here, as always"
    assert promo.requested_by_agent_id == report.agents["governor"] and promo.evidence["runtime_fallback"] is True
    gov_run = db.query(AgentRun).filter(AgentRun.role == "governor").order_by(AgentRun.created_at.desc()).first()
    assert promo.requested_by_run_id == gov_run.id
    rec = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.READY_REOFFER_SETTLED).one()
    assert (rec.payload["fallback"], rec.payload["promotion_id"]) == ("requested", str(promo.id))
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.PROMOTION_REQUESTED).count() == 1


def test_the_fallback_obeys_the_governors_policy_and_the_change_budget(db, SessionLocal, society_settings, monkeypatch, grants_with_no_cooldown):
    settings = _settings(monkeypatch, SOCIETY_PROMOTION_PROVIDER="fake", SOCIETY_MAX_PROMOTIONS_PER_DAY=6, SOCIETY_MAX_OPEN_AUTONOMOUS_PRS=5,
                         SOCIETY_READY_FALLBACK_ENABLED="true")
    report = seed_society(db)
    grants_with_no_cooldown()
    db.execute(text("UPDATE agent_capability_grants SET approval_required_intents = '[\"REQUEST_PR_PROMOTION\"]'::jsonb WHERE role = 'governor'"))
    db.commit()
    _reoffer_and_idle_governor(db, SessionLocal, settings, report)
    promotion_queue.settle_reoffers(db, settings)
    rec = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.READY_REOFFER_SETTLED).one()
    assert rec.payload["fallback"].startswith("policy_approval_required") and db.query(CodePromotion).count() == 0, "a human gate stays a human gate"

    full = dataclasses.replace(settings, max_open_autonomous_prs=0)
    db.execute(text("UPDATE agent_capability_grants SET approval_required_intents = '[]'::jsonb WHERE role = 'governor'"))
    db.commit()
    _reoffer_and_idle_governor(db, SessionLocal, settings, report, "second")
    promotion_queue.settle_reoffers(db, full)
    last = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.READY_REOFFER_SETTLED).order_by(SocietyEvent.created_at.desc()).first()
    assert last.payload["fallback"] == "no_capacity" and db.query(CodePromotion).count() == 0


# ── 3. keep every role busy ─────────────────────────────────────────────


def _approved_ticket(db, report, minutes_ago=15):
    p = ImprovementProposal(id=uuid.uuid4(), proposed_by_agent_id=report.agents["scout"], source="audit", title="approved, nobody designs it",
                            problem="p", proposed_change="c", status="APPROVED", target_scope="platform", importance=60)
    db.add(p)
    db.flush()
    tid = tickets.create(db, proposal_id=p.id, title=p.title, fields={"objective_id": "O5", "metric_id": "verified_signups_7d", "expected_effect": 3,
                         "direction": "up", "proof": ["probe:verified_signups_7d"]}, source="scout", role="scout", importance=60)
    tickets.set_status(db, tid, "approved")
    t = tickets.for_proposal(db, p.id)
    tickets.emit_approved(db, t, None, by="board")
    db.execute(text("UPDATE society_events SET created_at = NOW() - make_interval(mins => :m) WHERE payload ->> 'ticket_id' = :t"), {"m": minutes_ago, "t": str(tid)})
    db.commit()
    return tid


def test_an_approved_ticket_nobody_designs_wakes_the_architect_again_without_resetting_its_clock(db, company):
    report, settings = company
    tid = _approved_ticket(db, report)
    approved_at = tickets._approved_at(db, tid)
    assert tickets.wake_undesigned(db, utcnow()) == 1
    db.commit()
    wakes = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.COMPANY_TICKET_APPROVED).order_by(SocietyEvent.created_at).all()
    assert [w.payload.get("rewake") for w in wakes] == [None, 1] and wakes[1].payload["ticket_id"] == str(tid)
    assert tickets._approved_at(db, tid) == approved_at, "a wake is not an approval: the design-failure count and the 6h clock stand"
    assert tickets.wake_undesigned(db, utcnow()) == 0, "the new wake gets its own window"
    db.execute(text("UPDATE society_events SET created_at = created_at - interval '11 minutes' WHERE event_type = 'company.ticket_approved'"))
    db.commit()
    assert tickets.wake_undesigned(db, utcnow()) == 1
    db.commit()
    db.execute(text("UPDATE society_events SET created_at = created_at - interval '11 minutes' WHERE event_type = 'company.ticket_approved'"))
    db.commit()
    assert tickets.wake_undesigned(db, utcnow()) == 0, f"at most {tickets.MAX_DESIGN_REWAKES} re-sent wakes; then the stale sweep counts the attempt"
    assert tickets.for_correlation(db, wakes[1].correlation_id)["id"] == tid, "the Architect's story designs that ticket"


def test_a_running_design_story_is_left_alone(db, company):
    report, settings = company
    tid = _approved_ticket(db, report)
    ev = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.COMPANY_TICKET_APPROVED).one()
    db.add(AgentRun(id=uuid.uuid4(), agent_id=report.agents["architect"], event_id=ev.id, correlation_id=ev.correlation_id, status="running", role="architect"))
    db.commit()
    assert tickets.wake_undesigned(db, utcnow()) == 0
    assert tid


def test_the_architects_precheck_counts_a_ticket_waiting_for_its_design(db, company):
    report, settings = company
    agent_id = report.agents["architect"]
    hb = emit_event(db, event_type=EventType.SOCIETY_HEARTBEAT, payload={}, idempotency_key="t-hb-arch")
    db.add(AgentRun(id=uuid.uuid4(), agent_id=agent_id, event_id=hb.id, correlation_id=hb.correlation_id, status="completed", role="architect", completed_at=utcnow()))
    db.commit()
    busy, why = wake_precheck.heartbeat_work(db, agent_id=agent_id, role="architect", now=utcnow(), max_idle_hours=6)
    assert not busy and "no ticket to design" in why
    _approved_ticket(db, report)
    busy, why = wake_precheck.heartbeat_work(db, agent_id=agent_id, role="architect", now=utcnow(), max_idle_hours=6)
    assert busy and why == "1 approved ticket(s) without a design"


# ── 4. throughput limits the owner sets are reachable ───────────────────


def _validator():
    spec = importlib.util.spec_from_file_location("phase5_live_g1v", ROOT / "deploy" / "railway" / "phase5_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_validator_sets_a_roles_runs_per_hour_and_a_reseed_keeps_it(db, society_settings):
    mod = _validator()
    assert mod.parse_plan("runs:governor:40") == [("runs", ["governor", "40"])]
    assert mod.parse_plan("runs:architect:120") == [("runs", ["architect", "120"])]
    for bad in ("runs:governor", "runs:governor:0", "runs:governor:121", "runs:governor:201", "runs:Gov!:5"):
        try:
            mod.parse_plan(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    seed_society(db)
    grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.role == "governor").one()
    assert grant.max_runs_per_hour == DEFAULT_ROLES["governor"].max_runs_per_hour == 12
    grant.max_runs_per_hour = 40  # what `runs:governor:40` writes
    db.commit()
    seed_society(db)  # every deploy re-seeds
    db.expire_all()
    assert db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.role == "governor").one().max_runs_per_hour == 40
    assert DEFAULT_ROLES["governor"].max_intents_per_run >= tickets.OWNER_STORY_TICKETS + 1, "5 tickets and the reply fit one decision"


def test_the_prompt_asks_the_governor_to_decompose_the_owners_words():
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "Decompose the owner's words into tickets NOW: the Governor files 1..5" in flat
    assert '"owner_request.rewake" means this story ended with no work' in flat
    assert "1..5 tickets" in owner_thread.LABEL and owner_thread.WITHOUT_WORK in owner_thread.LABEL
    assert timedelta(seconds=owner_thread.SLO_TARGETS["message_to_plan"]) == timedelta(minutes=10)
