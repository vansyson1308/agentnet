"""A READY candidate either gets a promotion or a structured reason (society/promotion_queue.py).

Staging 2026-10-27: 438d8ab0 got a promotion (shadow PR #115); feb64aa6 went READY too and never
got one -- REQUEST_PR_PROMOTION ran twice, 1 promotion in 24h. A refused request (a change budget)
was never asked again, and nothing said why the candidate waited."""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import uuid

from sqlalchemy import text

from services.registry.app.models import AgentIntent, CodeCandidate, CodePromotion, SocietyEvent
from services.registry.app.society import daily_report, promotion, promotion_queue
from services.registry.app.society.cognition import FakeModel
from services.registry.app.society.events import EventType, emit_event, utcnow
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker

from .test_redelivery import _settings

IDLE = {"decision_summary": "observe", "intents": [], "sleep_for_seconds": 0}


def _ready(db, report, title="ready candidate"):
    cand = CodeCandidate(id=uuid.uuid4(), correlation_id=uuid.uuid4(), requested_by_agent_id=report.agents["architect"], builder_agent_id=report.agents["builder"],
                         title=title, spec={"kind": "code", "files_allowed": ["src/a.py"], "acceptance_tests": ["tests/test_a.py"]}, status="ready", risk_tier="green",
                         head_sha="h" * 40, base_sha="b" * 40)
    db.add(cand)
    db.flush()
    emit_event(db, event_type=EventType.CODE_CANDIDATE_READY, payload={"candidate_id": str(cand.id), "title": cand.title, "qa_summary": "pass"}, actor_type="agent",
               actor_id=report.agents["security"], subject_type="code_candidate", subject_id=cand.id, correlation_id=cand.correlation_id, idempotency_key=f"t-ready-{cand.id}")
    db.commit()
    return cand


def _promote(db, report, cand, status="pr_open"):
    p = CodePromotion(id=uuid.uuid4(), candidate_id=cand.id, correlation_id=cand.correlation_id, risk_tier="green", base_sha=cand.base_sha, candidate_sha=cand.head_sha,
                      provider="fake", status=status, requested_by_agent_id=report.agents["governor"], eligibility={}, evidence={})
    db.add(p)
    db.commit()
    return p


def _age(db, cand, hours=2):
    db.execute(text("UPDATE society_events SET created_at = created_at - make_interval(hours => :h), status = 'processed' WHERE subject_id = :c"), {"h": hours, "c": cand.id})
    db.commit()


def _governor_requests(db, SessionLocal, settings, event_type=EventType.CODE_CANDIDATE_READY):
    """The Governor, woken by READY, requests the PR for the candidate its wake names."""
    def decide(ctx):
        data = ctx.event["payload"].get("data") or ctx.event["payload"]
        if ctx.role == "governor" and ctx.event["type"] == event_type and data.get("candidate_id"):
            return {"decision_summary": "promote", "intents": [{"type": "REQUEST_PR_PROMOTION", "payload": {"candidate_id": data["candidate_id"]}}], "sleep_for_seconds": 0}
        return IDLE

    worker = SocietyWorker(SessionLocal, settings=settings, model=FakeModel(decide), worker_id="w-promote", telemetry_enabled=False)
    worker.routing = {event_type: ["governor"]}
    asyncio.run(worker.run_until_idle(max_cycles=6))
    db.expire_all()


def test_a_ready_candidate_refused_by_the_daily_cap_is_reoffered_when_capacity_frees(db, SessionLocal, society_settings, monkeypatch, grants_with_no_cooldown):
    settings = _settings(monkeypatch, SOCIETY_PROMOTION_PROVIDER="fake", SOCIETY_MAX_PROMOTIONS_PER_DAY=1, SOCIETY_MAX_OPEN_AUTONOMOUS_PRS=3)
    report = seed_society(db)
    grants_with_no_cooldown()
    first = _ready(db, report, "O5 register/landing")
    shadow = _promote(db, report, first)  # the day's one promotion (#115)
    cand = _ready(db, report, "O4 quote and accept a paid USDC task")
    _governor_requests(db, SessionLocal, settings)
    (req,) = db.query(AgentIntent).filter(AgentIntent.intent_type == "REQUEST_PR_PROMOTION", AgentIntent.payload["candidate_id"].astext == str(cand.id)).all()
    assert req.execution_status.value == "failed" and promotion_queue.CAP_MARK in req.error
    (b,) = promotion_queue.ready_backlog(db, settings)
    assert (b["candidate"], b["reason"], b["requests"]) == (str(cand.id)[:8], "cap_promotions_per_day", 1)
    assert promotion_queue.retry_deferred(db, settings) == 0, "no capacity: nothing is re-offered"
    # the owner closed #115 yesterday; the day turned
    db.execute(text("UPDATE code_promotions SET status = 'rejected', created_at = NOW() - INTERVAL '1 day' WHERE id = :p"), {"p": shadow.id})
    db.commit()
    _age(db, cand)
    backlog = {x["candidate"]: x["reason"] for x in promotion_queue.ready_backlog(db, settings)}
    assert backlog == {str(first.id)[:8]: "promotion_rejected", str(cand.id)[:8]: "refused"}, "an ended promotion is a reason, never re-offered"
    assert promotion_queue.retry_deferred(db, settings) == 1
    assert promotion_queue.retry_deferred(db, settings) == 0, "once a day"
    (wake,) = db.query(SocietyEvent).filter(SocietyEvent.idempotency_key.like(promotion_queue.KEY_PREFIX + "%")).all()
    assert wake.subject_id == cand.id and wake.payload["promotion_retry"] == "refused" and wake.actor_type == "system" and wake.correlation_id != cand.correlation_id
    _governor_requests(db, SessionLocal, settings)
    (promo,) = db.query(CodePromotion).filter(CodePromotion.candidate_id == cand.id).all()
    assert promo.requested_by_agent_id == report.agents["governor"], "the Governor's request, through the controller"
    backlog = {x["candidate"]: x["reason"] for x in promotion_queue.ready_backlog(db, settings)}
    assert str(cand.id)[:8] not in backlog or backlog[str(cand.id)[:8]].startswith("promotion_"), "promoted, or a promotion that ended says so"



def test_reasons_and_bounds(db, society_settings, monkeypatch, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    a, b = _ready(db, report, "a"), _ready(db, report, "b")
    off = _settings(monkeypatch, SOCIETY_PROMOTION_PROVIDER="disabled")
    assert {x["reason"] for x in promotion_queue.ready_backlog(db, off)} == {"provider_disabled"}
    assert promotion_queue.retry_deferred(db, off) == 0
    on = _settings(monkeypatch, SOCIETY_PROMOTION_PROVIDER="fake", SOCIETY_MAX_PROMOTIONS_PER_DAY=10, SOCIETY_MAX_OPEN_AUTONOMOUS_PRS=2)
    assert {x["reason"] for x in promotion_queue.ready_backlog(db, on)} == {"awaiting_request"}
    assert promotion_queue.retry_deferred(db, on) == 0, "the Governor still has its READY wake (< 1h)"
    _promote(db, report, _ready(db, report, "open"))
    assert {x["reason"] for x in promotion_queue.ready_backlog(db, on) if x["candidate_id"] in (str(a.id), str(b.id))} == {"awaiting_request"}
    _age(db, a), _age(db, b)
    assert promotion_queue.retry_deferred(db, on) == 1, "one open PR slot left: only the oldest is re-offered"
    retried = db.query(SocietyEvent).filter(SocietyEvent.idempotency_key.like(promotion_queue.KEY_PREFIX + "%")).one()
    assert retried.subject_id == a.id
    _promote(db, report, _ready(db, report, "open 2"))
    assert {x["reason"] for x in promotion_queue.ready_backlog(db, on)} == {"cap_open_prs"}


def test_the_daily_report_and_the_validator_name_ready_candidates_without_a_pr(db, engine, society_settings, monkeypatch, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    monkeypatch.setenv("SOCIETY_PROMOTION_PROVIDER", "fake")
    cand = _ready(db, report)
    _settings(monkeypatch)
    rep = daily_report.build(db, utcnow())
    assert rep["data"]["ready_without_pr"] == [{"candidate": str(cand.id)[:8], "reason": "awaiting_request", "requests": 0}]
    assert f"READY without a PR: {str(cand.id)[:8]} (awaiting_request)" in rep["text"]
    spec = importlib.util.spec_from_file_location("phase5_live_pq", pathlib.Path(__file__).resolve().parents[2] / "deploy/railway/phase5_live.py")
    p5 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p5)
    assert set(p5.ACTIVE_PROMOTION) == {s.value for s in promotion.ACTIVE_STATUSES}
    conn = engine.raw_connection()
    try:
        with conn.cursor() as cur:
            (w,) = p5.ready_without_promotion(cur)
        conn.rollback()
    finally:
        conn.close()
    assert (w["candidate"], w["requests"], w["last_request"], w["ended_promotion"], w["ready_wakes"]) == (str(cand.id)[:8], 0, None, None, 1)
