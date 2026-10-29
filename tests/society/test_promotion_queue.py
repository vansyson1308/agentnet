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


# ── owner 2026-10-29: READY -> PR. The closed red PR #115 held the only open-PR slot ──────


def _budget_settings(monkeypatch, temp_repo, tmp_path, **env):
    return _settings(monkeypatch, SOCIETY_RUNTIME_ENABLED="true", SOCIETY_AUTONOMOUS_CODE_ENABLED="true", SOCIETY_REPO_ROOT=str(temp_repo),
                     SOCIETY_WORKSPACE_ROOT=str(tmp_path / "ws"), SOCIETY_PROMOTION_POLL_INTERVAL_SECONDS=0, SOCIETY_PROMOTION_PROVIDER="fake", **env)


def _red_pr(db, db_factory, settings, report, provider, title, path):
    from .test_promotion import _request, drive, ready_candidate

    cand = ready_candidate(db, settings, report, path=path, title=title)
    promo, _ = _request(db, settings, cand, report)
    provider.set_ci(cand.branch_name, "failed")
    drive(db_factory, settings, provider)
    db.refresh(promo)
    assert promo.status.value == "ci_failed"
    return cand, promo


def test_a_red_pr_the_owner_closed_ends_its_promotion_and_frees_the_slot(db, db_factory, temp_repo, tmp_path, monkeypatch):
    from .test_promotion import _request, drive, ready_candidate

    settings = _budget_settings(monkeypatch, temp_repo, tmp_path, SOCIETY_MAX_OPEN_AUTONOMOUS_PRS=1, SOCIETY_MAX_PROMOTIONS_PER_DAY=5)
    report = seed_society(db)
    provider = promotion.FakePromotionProvider()
    red, p1 = _red_pr(db, db_factory, settings, report, provider, "shadow #115", "docs/society/candidates/one.md")
    waiting = ready_candidate(db, settings, report, path="docs/society/candidates/two.md", title="O4 doc")
    assert promotion.open_prs(db) == 1
    try:
        _request(db, settings, waiting, report)
        raise AssertionError("the cap must refuse while the red PR is open")
    except ValueError as exc:
        assert "autonomous PRs already open" in str(exc)
    provider.close(p1.external_pr_number)
    drive(db_factory, settings, provider)
    db.refresh(p1), db.refresh(red)
    assert p1.status.value == "rejected" and p1.failure_reason.startswith(promotion.CLOSED_BY_OWNER)
    assert red.status.value == "rejected" and f"PR #{p1.external_pr_number}" in red.error, "a closed PR ends its candidate too"
    assert promotion.open_prs(db) == 0, "the slot is free"
    promo, created = _request(db, settings, waiting, report)
    assert created and promo.status.value == "requested", "the waiting READY candidate is promoted"
    assert provider.merge_count == 0


def test_a_red_pr_merged_by_a_human_or_rerun_green_is_followed(db, db_factory, temp_repo, tmp_path, monkeypatch):
    from .test_promotion import drive

    settings = _budget_settings(monkeypatch, temp_repo, tmp_path, SOCIETY_MAX_OPEN_AUTONOMOUS_PRS=3, SOCIETY_MAX_PROMOTIONS_PER_DAY=5)
    report = seed_society(db)
    provider = promotion.FakePromotionProvider()
    merged, p1 = _red_pr(db, db_factory, settings, report, provider, "merged by hand", "docs/society/candidates/m.md")
    rerun, p2 = _red_pr(db, db_factory, settings, report, provider, "re-run", "docs/society/candidates/r.md")
    provider.human_merge(p1.external_pr_number)
    provider.set_ci(rerun.branch_name, "passed")
    drive(db_factory, settings, provider)
    db.refresh(p1), db.refresh(p2), db.refresh(merged), db.refresh(rerun)
    assert p1.status.value == "merged" and merged.status.value == "merged", "the change is on main: the candidate is no longer READY"
    assert p2.status.value in ("ci_passed", "awaiting_approval"), p2.status
    seq = [e.event_type for e in db.query(SocietyEvent).filter(SocietyEvent.correlation_id == rerun.correlation_id).order_by(SocietyEvent.created_at, SocietyEvent.id).all()]
    assert seq.index("promotion.ci_failed") < seq.index("promotion.ci_pending") < seq.index("promotion.ci_passed"), "never green before CI ran again"
    assert rerun.status.value == "ready" and provider.merge_count == 0


def test_stale_ready_rows_are_settled_to_their_terminal_status(db, society_settings, monkeypatch, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    on = _settings(monkeypatch, SOCIETY_PROMOTION_PROVIDER="fake", SOCIETY_MAX_OPEN_AUTONOMOUS_PRS=3)
    was_merged, was_closed, was_red, waiting = (_ready(db, report, t) for t in ("#30", "#115", "ended ci_failed", "O4"))
    _promote(db, report, was_merged, "merged")
    p = _promote(db, report, was_closed, "rejected")
    p.failure_reason, p.external_pr_number = "pull request closed without merge", 115  # a row from before closed_by_owner
    _promote(db, report, was_red, "rejected").failure_reason = "rejected: CI failed after 3 attempts"
    db.commit()
    assert promotion_queue.settle_ended(db) == 2
    assert promotion_queue.settle_ended(db) == 0, "idempotent"
    db.expire_all()
    assert (was_merged.status.value, was_closed.status.value, was_red.status.value, waiting.status.value) == ("merged", "rejected", "ready", "ready")
    assert was_closed.error.startswith(promotion.CLOSED_BY_OWNER) and "PR #115" in was_closed.error
    reasons = {x["candidate_id"]: x["reason"] for x in promotion_queue.ready_backlog(db, on)}
    assert reasons == {str(was_red.id): "promotion_rejected", str(waiting.id): "awaiting_request"}, "ready=N now means real work"


def test_a_base_that_left_main_is_named_stale_and_never_reoffered(db, society_settings, temp_repo, monkeypatch, grants_with_no_cooldown):
    import subprocess

    report = seed_society(db)
    grants_with_no_cooldown()
    on = _settings(monkeypatch, SOCIETY_PROMOTION_PROVIDER="fake", SOCIETY_MAX_OPEN_AUTONOMOUS_PRS=3, SOCIETY_REPO_ROOT=str(temp_repo))
    git = lambda *a: subprocess.run(["git", *a], cwd=temp_repo, check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
    main = git("rev-parse", "HEAD")
    git("checkout", "-q", "-b", "elsewhere")
    git("commit", "-q", "--allow-empty", "-m", "off main")
    gone = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    git("commit", "-q", "--allow-empty", "-m", "main moves on")
    stale, behind = _ready(db, report, "stale"), _ready(db, report, "behind")
    stale.base_sha, behind.base_sha = gone, main
    db.commit()
    _age(db, stale), _age(db, behind)
    items = {x["candidate_id"]: x for x in promotion_queue.ready_backlog(db, on)}
    assert items[str(stale.id)]["reason"] == "stale_base" and items[str(stale.id)]["base_behind_main"] is None
    assert items[str(behind.id)]["reason"] == "awaiting_request" and items[str(behind.id)]["base_behind_main"] == 1, "behind main is fine: the PR is reconciled"
    assert promotion_queue.retry_deferred(db, on) == 1
    (wake,) = db.query(SocietyEvent).filter(SocietyEvent.idempotency_key.like(promotion_queue.KEY_PREFIX + "%")).all()
    assert wake.subject_id == behind.id


def test_the_change_budget_is_reported_by_the_heartbeat_the_report_and_the_validator(db, engine, society_settings, monkeypatch, grants_with_no_cooldown):
    from services.registry.app.society import world

    report = seed_society(db)
    grants_with_no_cooldown()
    on = _settings(monkeypatch, SOCIETY_PROMOTION_PROVIDER="fake", SOCIETY_MAX_OPEN_AUTONOMOUS_PRS=3, SOCIETY_MAX_PROMOTIONS_PER_DAY=2, SOCIETY_HEARTBEAT_INTERVAL_SECONDS=1)
    _promote(db, report, _ready(db, report, "open"), "ci_failed")
    caps = promotion_queue.caps(db, on)
    assert caps == {"max_promotions_per_day": 2, "promotions_today": 1, "max_open_autonomous_prs": 3, "open_prs": 1, "provider": "fake", "auto_merge_enabled": False}
    world.emit_heartbeat(db, on)
    hb = db.query(SocietyEvent).filter(SocietyEvent.event_type.in_(["society.heartbeat", "society.heartbeat.idle"])).order_by(SocietyEvent.created_at.desc()).first()
    assert hb is not None and hb.payload["change_budget"] == caps
    text_ = daily_report.build(db, utcnow())["text"]
    assert "Change budget: 1/2 promotions today, 1/3 autonomous PRs open (provider fake, auto-merge off)" in text_
    spec = importlib.util.spec_from_file_location("phase5_live_cb", pathlib.Path(__file__).resolve().parents[2] / "deploy/railway/phase5_live.py")
    p5 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p5)
    assert set(p5.OPEN_PR) == {s.value for s in promotion.OPEN_PR_STATUSES}
    conn = engine.raw_connection()
    try:
        with conn.cursor() as cur:
            budget = p5.change_budget(cur)
        conn.rollback()
    finally:
        conn.close()
    assert budget["caps"] == caps and budget["open_prs"] == 1 and budget["ci_failed"] == 1 and budget["ready_ended"] == 0
