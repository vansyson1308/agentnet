"""Words -> work (owner 2026-10-29, staging 56c7129): six owner messages, six polite replies, zero
work started. The Governor said "only e14a7bb1 is READY" and "1 autonomous PR is open" while /owner
showed 3 READY and 0/5 PRs: it reasoned from stale memory. Now an owner story carries the LIVE
portfolio read at run time, the Governor's reply rides with the intents that start the work (each plan
step linked to one), READY candidates are re-offered as soon as capacity appears, and the throughput
funnel says where work waits. No new authority: the same intents, policy, budgets and controller."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import pathlib
import uuid
from datetime import timedelta

from sqlalchemy import text

from services.registry.app.models import Agent, AgentCapabilityGrant, AgentIntent, CodePromotion, MemoryItem, MemoryScope, SocietyEvent
from services.registry.app.society import daily_report, funnel, owner_thread, promotion_queue
from services.registry.app.society.cognition import FakeModel
from services.registry.app.society.context import build_context
from services.registry.app.society.events import EventType, emit_event, utcnow
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker

from .conftest import auth
from .test_promotion_queue import _age, _promote, _ready
from .test_redelivery import _settings

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _owner_event(db, entry):
    return db.query(SocietyEvent).filter(SocietyEvent.idempotency_key == f"owner:{entry['id']}").one()


def _governor(db, report):
    agent = db.query(Agent).filter(Agent.id == report.agents["governor"]).one()
    return agent, db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == agent.id).one()


# ── 1. live truth in owner stories ──────────────────────────────────────


def test_an_owner_story_carries_the_live_portfolio_not_stale_memory(db, society_settings, monkeypatch, grants_with_no_cooldown):
    settings = _settings(monkeypatch, SOCIETY_PROMOTION_PROVIDER="fake", SOCIETY_MAX_PROMOTIONS_PER_DAY=6, SOCIETY_MAX_OPEN_AUTONOMOUS_PRS=5)
    report = seed_society(db)
    grants_with_no_cooldown()
    ready = [_ready(db, report, f"READY {i}") for i in range(3)]
    agent, grant = _governor(db, report)
    db.add(MemoryItem(id=uuid.uuid4(), agent_id=agent.id, scope=MemoryScope.AGENT, title="promotion state", content="only e14a7bb1 is READY; 1 autonomous PR is already open",
                      importance=90, source_type="run", author_agent_id=agent.id))
    entry = owner_thread.record(db, kind="message", text_="request PRs for the 3 READY candidates", user_id=None)
    db.commit()
    ctx = build_context(db, agent=agent, grant=grant, event=_owner_event(db, entry), run=None, settings=settings)
    live = ctx.owner_request["portfolio"]
    assert "e14a7bb1" in str(ctx.memory), "the stale memory is still there ..."
    assert {r["candidate_id"] for r in live["ready_candidates"]} == {str(c.id) for c in ready}, "... but the live block names the real READY set"
    assert all(r["reason"] == "awaiting_request" for r in live["ready_candidates"])
    assert (live["change_budget"]["open_prs"], live["change_budget"]["max_open_autonomous_prs"], live["change_budget"]["provider"]) == (0, 5, "fake")
    assert live["label"].startswith("LIVE DATA") and "this block is right" in live["label"]
    assert live["budget"]["daily_model_budget_usd"] == float(settings.daily_model_budget_usd) and {r["role"] for r in live["budget"]["roles"]} >= {"governor", "scout"}
    _promote(db, report, ready[0])  # the truth changes: the next run sees it, nothing is cached
    ctx = build_context(db, agent=agent, grant=grant, event=_owner_event(db, entry), run=None, settings=settings)
    assert ctx.owner_request["portfolio"]["change_budget"]["open_prs"] == 1
    plain = emit_event(db, event_type="t.plain", payload={}, idempotency_key="t-plain")
    db.commit()
    assert build_context(db, agent=agent, grant=grant, event=plain, run=None, settings=settings).owner_request == {}, "only an owner story carries it"


# ── 2. words -> work ────────────────────────────────────────────────────


def test_the_governor_starts_work_in_the_same_decision_and_each_step_links_to_it(db, SessionLocal, society_settings, monkeypatch, grants_with_no_cooldown):
    settings = _settings(monkeypatch, SOCIETY_PROMOTION_PROVIDER="fake", SOCIETY_MAX_PROMOTIONS_PER_DAY=6, SOCIETY_MAX_OPEN_AUTONOMOUS_PRS=5)
    report = seed_society(db)
    grants_with_no_cooldown()
    cands = [_ready(db, report, f"READY {i}") for i in range(2)]
    for c in cands:
        _age(db, c)
    entry = owner_thread.record(db, kind="message", text_="REQUEST_PR_PROMOTION for the READY candidates", user_id=None)
    db.commit()

    def decide(ctx):
        if ctx.role == "governor" and ctx.owner_request and not ctx.owner_request["answered"]:
            live = ctx.owner_request["portfolio"]["ready_candidates"]
            promote = [{"type": "REQUEST_PR_PROMOTION", "payload": {"candidate_id": r["candidate_id"]}} for r in live]
            steps = [{"step": f"open the PR for {r['title']}", "intent": i + 2} for i, r in enumerate(live)]  # intent 1 is the reply itself
            steps += [{"step": "nonsense link", "intent": 9}, "watch CI and report"]
            reply = {"type": "RESPOND_TO_OWNER", "payload": {"reply": f"Requested PRs for {len(live)} READY candidates; the controller opens them.",
                                                             "plan_steps": steps, "ticket_ids": [], "owner_actions_needed": []}}
            return {"decision_summary": "promote and answer", "intents": [reply] + promote, "sleep_for_seconds": 60}
        return {"decision_summary": "nothing", "intents": [], "sleep_for_seconds": 60}

    worker = SocietyWorker(SessionLocal, settings=settings, model=FakeModel(decide), worker_id="w-owner-work", telemetry_enabled=False)
    asyncio.run(worker.owner_lane_once())
    db.expire_all()
    assert {p.candidate_id for p in db.query(CodePromotion).all()} == {c.id for c in cands}, [
        (i.intent_type, _ev(i.policy_decision), _ev(i.execution_status), i.error, i.policy_reason) for i in db.query(AgentIntent).all()]
    (row,) = db.query(AgentIntent).filter(AgentIntent.intent_type == "RESPOND_TO_OWNER").all()
    assert _ev(row.execution_status) == "executed" and row.seq == 0, "the reply came first in the decision and still ran after the work"
    e = owner_thread.view(db)["entries"][0]
    assert e["id"] == entry["id"] and e["status"] == "answered"
    assert [(w["type"], w["status"]) for w in e["work"]] == [("REQUEST_PR_PROMOTION", "executed")] * 2
    assert all(w["result"].get("promotion_id") for w in e["work"])
    states = [(s["state"], s.get("type")) for s in e["reply"]["plan_steps"]]
    assert states == [("started", "REQUEST_PR_PROMOTION"), ("started", "REQUEST_PR_PROMOTION"), ("next", None), ("next", None)]
    assert e["reply"]["unlinked_steps"] == 1, "a step that names no intent of the decision is not claimed as started"
    slo = owner_thread.slo(db)
    assert slo["first_work"]["n"] == 1 and slo["first_work"]["ok"] is True


def test_an_answer_without_work_is_measured_late(db):
    owner_thread.record(db, kind="message", text_="start something", user_id=None, now=utcnow() - timedelta(minutes=10))
    db.commit()
    m = owner_thread.slo(db)["first_work"]
    assert m["n"] == 0 and m["late_unanswered"] == 1 and m["ok"] is False


# ── 3. re-offer as soon as capacity appears ─────────────────────────────


def test_a_refused_ready_candidate_is_reoffered_when_the_cap_is_raised_not_next_day(db, society_settings, monkeypatch, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    tight = _settings(monkeypatch, SOCIETY_PROMOTION_PROVIDER="fake", SOCIETY_MAX_PROMOTIONS_PER_DAY=6, SOCIETY_MAX_OPEN_AUTONOMOUS_PRS=1)
    _promote(db, report, _ready(db, report, "holds the only slot"))
    cand = _ready(db, report, "waits for a slot")
    _age(db, cand)
    assert promotion_queue.retry_deferred(db, tight) == 0, "no slot: nothing to offer"
    raised = _settings(monkeypatch, SOCIETY_PROMOTION_PROVIDER="fake", SOCIETY_MAX_PROMOTIONS_PER_DAY=6, SOCIETY_MAX_OPEN_AUTONOMOUS_PRS=5)
    assert promotion_queue.retry_deferred(db, raised) == 1, "the owner raised the cap: offered now"
    assert promotion_queue.retry_deferred(db, raised) == 0, "once per capacity change"
    # a PR ends (the owner closed it): capacity can have appeared -> offered again, even within the hour
    db.execute(text("UPDATE code_promotions SET status = 'rejected'"))
    db.commit()
    assert promotion_queue.retry_deferred(db, raised) == 1
    wakes = db.query(SocietyEvent).filter(SocietyEvent.idempotency_key.like(promotion_queue.KEY_PREFIX + "%")).order_by(SocietyEvent.created_at).all()
    assert [w.subject_id for w in wakes] == [cand.id, cand.id] and wakes[0].payload["capacity_token"] != wakes[1].payload["capacity_token"]
    assert all(w.actor_type == "system" and w.event_type == EventType.CODE_CANDIDATE_READY for w in wakes), "the Governor's usual wake: policy and the controller decide"


# ── 4. the funnel ───────────────────────────────────────────────────────


def test_the_funnel_counts_the_pipeline_and_names_the_bottleneck(db, society_settings, monkeypatch, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    a, b = _ready(db, report, "a"), _ready(db, report, "b")
    _promote(db, report, a)
    emit_event(db, event_type=EventType.PROMOTION_PR_OPEN, payload={}, subject_type="promotion", subject_id=uuid.uuid4(), idempotency_key="t-pr")
    db.execute(text("UPDATE code_candidates SET updated_at = NOW() - INTERVAL '5 hours' WHERE id = :b"), {"b": b.id})
    db.commit()
    f = funnel.funnel(db, hours=24)
    assert (f["ready"], f["prs_opened"], f["merged"]) == (2, 1, 0)
    assert f["usd_per_ready"] is None or f["usd_per_ready"] >= 0
    assert len(f["by_2h"]) == 12 and sum(x["ready"] for x in f["by_2h"]) == 2
    stages = {s["stage"]: s for s in f["stages"]}
    assert stages["ready_awaiting_pr"]["waiting"] == 1 and stages["pr_ci"]["waiting"] == 1
    assert f["bottleneck"]["stage"] == "ready_awaiting_pr" and f["bottleneck"]["median_h"] >= 4.9, "work waits longest for a PR"
    assert "bottleneck ready_awaiting_pr" in funnel.summary(f)
    rep = daily_report.build(db)
    assert rep["data"]["funnel"]["ready"] == 2 and "Funnel 24h:" in rep["text"]


def test_only_an_operator_reads_the_funnel(db, api_client, user_token):
    _, plain = user_token(None)
    _, op = user_token("operator")
    assert api_client.get("/v1/society/company/funnel").status_code == 401
    assert api_client.get("/v1/society/company/funnel", headers=auth(plain)).status_code == 403
    r = api_client.get("/v1/society/company/funnel?hours=12", headers=auth(op))
    assert r.status_code == 200 and r.json()["window_hours"] == 12 and len(r.json()["by_2h"]) == 6
    assert api_client.get("/v1/society/company/funnel?hours=1", headers=auth(op)).status_code == 422


def _p5():
    spec = importlib.util.spec_from_file_location("phase5_live_funnel", ROOT / "deploy/railway/phase5_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_validator_funnel_step(db, monkeypatch):
    import pytest

    p5 = _p5()
    assert p5.parse_plan("funnel:24") == [("funnel", ["24"])] and p5.parse_plan("funnel") == [("funnel", [])]
    for bad in ("funnel:1", "funnel:x", "funnel:24:2"):
        with pytest.raises(ValueError):
            p5.parse_plan(bad)
    body = funnel.funnel(db, hours=24)
    seen = []
    monkeypatch.setattr(p5, "api", lambda method, url, token, body_=None, timeout=30.0: (seen.append(url), (200, body))[1])
    checks, infos, blobs = [], [], {}

    class Out:
        def check(self, group, code, ok, detail=""):
            checks.append((code, bool(ok)))
            return bool(ok)

        def info(self, group, detail):
            infos.append(detail)

        def json(self, group, key, obj):
            blobs[key] = obj

    p5.step_funnel(Out(), "https://api.test", "operator-jwt", 24)
    assert seen == ["https://api.test/v1/society/company/funnel?hours=24"] and checks == [("F00", True), ("F01", True)]
    assert infos and "bottleneck" in infos[0] and blobs["funnel"]["window_hours"] == 24


def test_the_prompt_says_live_data_beats_memory_and_words_need_work():
    from services.registry.app.society.cognition import SYSTEM_PROMPT

    flat = " ".join(SYSTEM_PROMPT.split())
    assert "never from memory or an earlier reply" in flat and "Words -> work" in flat and "Never claim work started" in flat


def test_the_live_portfolio_reads_no_secret_and_grants_nothing(db, society_settings):
    seed_society(db)
    live = owner_thread.portfolio(db, dataclasses.replace(society_settings), utcnow() + timedelta(seconds=1))
    flat = str(live).lower()
    assert "api_key" not in flat and "token" not in flat.replace("capacity_token", "")
    assert set(live) == {"label", "as_of", "ready_candidates", "change_budget", "today_plan", "tickets_by_status", "tickets_in_flight", "candidates_in_flight", "budget"}
