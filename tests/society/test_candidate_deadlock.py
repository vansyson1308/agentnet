"""A re-submitted candidate is QA'd again; stalled and obsolete work reaches the operator.

Live since 2026-09-29, found 2026-10-09: the Builder re-submitted a candidate
after its first QA failure. It went back to BUILT but kept ``qa_report.verdict
= "fail"`` from the previous head. Every role read that verdict as "failed":
nobody issued REQUEST_QA, and the Builder may neither re-submit nor DECLINE a
BUILT candidate (``DECLINABLE_STATUSES``). The 18h audit: 57 runs, 100%
WRITE_MEMORY, the candidate frozen in BUILT and its proposal holding a
portfolio slot.

The replay below rebuilds that exact state from real worktree commits (QA
attempt 1 failed on head A, head B committed, BUILT with A's verdict, every
lifecycle event older than the stall window), shows the deadlock with the old
behaviour (no health sweep), then lets the worker's deterministic sweep run.
The scripted roles behave like the live ones: QA evaluates a BUILT candidate,
but treats a candidate whose verdict reads "fail" as failed and writes memory.
"""

from __future__ import annotations

import asyncio
import uuid
from sqlalchemy import text

from services.registry.app.models import AgentIntent, AgentRun, CodeCandidate, ImprovementProposal, SocietyEvent
from services.registry.app.society import candidate_health
from services.registry.app.society.cognition import FakeModel
from services.registry.app.society.company import portfolio_accounting
from services.registry.app.society.events import EventType, emit_event
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker

from .conftest import auth

TEXTUTIL = "app/textutil.py"
OLD_TRUE = '_TRUE = {"true", "1"}'
PARTIAL_FIX = {"path": TEXTUTIL, "replacements": [{"old": OLD_TRUE, "new": '_TRUE = {"true", "1", "yes"}'}]}
FULL_FIX = {"path": TEXTUTIL, "replacements": [{"old": '_TRUE = {"true", "1", "yes"}', "new": '_TRUE = {"true", "1", "yes", "on", "y"}'}]}
SLEEP = {"decision_summary": "nothing to do", "intents": [], "sleep_for_seconds": 60}


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _seed(db, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    return report


def _proposal_and_candidate(db, report, *, status="requested", correlation_id=None):
    proposal = ImprovementProposal(
        id=uuid.uuid4(), proposed_by_agent_id=report.agents["scout"], source="audit",
        title="parse_bool rejects affirmative words", problem="tasks fail on 'yes'/'on'",
        proposed_change="accept affirmative words", status="CONVERTED_TO_TASK", target_scope="platform", importance=70,
    )
    db.add(proposal)
    db.flush()
    cand = CodeCandidate(
        id=uuid.uuid4(), proposal_id=proposal.id, correlation_id=correlation_id or uuid.uuid4(),
        requested_by_agent_id=report.agents["architect"], title="parse_bool: accept affirmative words",
        spec={
            "kind": "code", "description": "accept yes/on/y", "expected_effect": "affirmative words parse as True",
            "files_allowed": [TEXTUTIL], "acceptance_tests": ["tests/acceptance/test_parse_bool_regression.py"], "must_compile": True,
        },
        status=status, risk_tier="green",
    )
    db.add(cand)
    db.commit()
    return proposal, cand


def _act(db, SessionLocal, settings, role, intents, *, max_cycles=8):
    """One decision by ``role`` on a test event routed only to it; every other
    role does nothing (so the test decides who acts)."""
    ev = emit_event(db, event_type="t.act", payload={}, correlation_id=uuid.uuid4(), idempotency_key=f"t-act-{uuid.uuid4()}")
    db.commit()
    model = FakeModel({role: [{"decision_summary": "act", "intents": intents, "sleep_for_seconds": 1}]})
    worker = SocietyWorker(SessionLocal, settings=settings, model=model, worker_id=f"w-{role}", telemetry_enabled=False)
    worker.routing = {**worker.routing, "t.act": [role]}
    asyncio.run(worker.run_until_idle(max_cycles=max_cycles))
    db.expire_all()
    run = db.query(AgentRun).filter(AgentRun.event_id == ev.id).one()
    return db.query(AgentIntent).filter(AgentIntent.run_id == run.id).one()


def _submit(cand, edit, summary):
    return {"type": "SUBMIT_CODE_CANDIDATE", "payload": {"candidate_id": str(cand.id), "edits": [edit], "summary": summary}}


def _evaluate(candidate_id):
    return {"type": "EVALUATE_CODE_CANDIDATE", "payload": {"candidate_id": str(candidate_id)}}


def _live_roles(ctx):
    """QA and the Builder as the live audit saw them act."""
    et, role = ctx.event["type"], ctx.role
    data = (ctx.event.get("payload") or {}).get("data") or {}
    cand = next((c for c in ctx.candidates if c["id"] == data.get("candidate_id")), None) or (ctx.candidates[0] if ctx.candidates else None)
    if cand is None or role not in ("qa", "builder"):
        return SLEEP
    verdict = (cand.get("qa") or {}).get("verdict")
    if role == "qa" and et == EventType.CODE_CANDIDATE_BUILT and cand["status"] == "built" and verdict is None:
        return {"decision_summary": "evaluate the new head", "intents": [_evaluate(cand["id"])], "sleep_for_seconds": 60}
    if verdict == "fail":
        memory = {"title": f"candidate {cand['id'][:8]} failed QA", "content": "QA verdict is fail; nothing to do until someone re-designs it", "tags": ["qa"]}
        return {"decision_summary": "it failed", "intents": [{"type": "WRITE_MEMORY", "payload": memory}], "sleep_for_seconds": 60}
    return SLEEP


def _backdate(db, cand, days):
    db.execute(text("UPDATE society_events SET created_at = created_at - make_interval(days => :d) WHERE subject_id = :c"), {"d": days, "c": cand.id})
    db.execute(text("UPDATE code_candidates SET created_at = created_at - make_interval(days => :d) WHERE id = :c"), {"d": days, "c": cand.id})
    db.commit()


def _qa_requests(db, cand):
    return [e for e in db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.CODE_CANDIDATE_BUILT, SocietyEvent.subject_id == cand.id).all() if (e.payload or {}).get("qa_request")]


def _built_then_failed(db, SessionLocal, settings, report, cand):
    """Head A: a partial fix. QA attempt 1 fails it (QA_FAILED)."""
    assert _ev(_act(db, SessionLocal, settings, "builder", [_submit(cand, PARTIAL_FIX, "accept 'yes'")]).execution_status) == "executed"
    assert _ev(_act(db, SessionLocal, settings, "qa", [_evaluate(cand.id)]).execution_status) == "executed"
    cand = db.get(CodeCandidate, cand.id)
    assert _ev(cand.status) == "qa_failed" and cand.qa_report["verdict"] == "fail" and cand.qa_report["attempts"] == 1
    return cand


def test_replay_of_the_live_deadlock_state(db, SessionLocal, code_settings, grants_with_no_cooldown, monkeypatch):
    report = _seed(db, grants_with_no_cooldown)
    proposal, cand = _proposal_and_candidate(db, report)
    cand = _built_then_failed(db, SessionLocal, code_settings, report, cand)
    head_a, failed_report = cand.head_sha, dict(cand.qa_report)
    assert _ev(_act(db, SessionLocal, code_settings, "builder", [_submit(cand, FULL_FIX, "accept 'on' and 'y' too")]).execution_status) == "executed"

    # ── the exact production state (what the pre-fix re-submit left behind) ──
    cand = db.get(CodeCandidate, cand.id)
    head_b = cand.head_sha
    assert head_b != head_a and _ev(cand.status) == "built"
    cand.qa_report = failed_report  # verdict=fail, judged head A
    db.query(SocietyEvent).filter(SocietyEvent.subject_id == cand.id, SocietyEvent.payload.has_key("qa_request")).delete(synchronize_session=False)  # noqa: W601
    db.commit()
    _backdate(db, cand, days=10)  # lifecycle last moved on 2026-09-29
    assert candidate_health.has_stale_verdict(db.get(CodeCandidate, cand.id))

    # ── with the old behaviour, every wake ends in WRITE_MEMORY ──
    monkeypatch.setattr(candidate_health, "sweep", lambda db, settings, now=None: {})
    beats = []
    for _ in range(3):
        beats.append(emit_event(db, event_type=EventType.SOCIETY_HEARTBEAT, payload={}, correlation_id=uuid.uuid4(), idempotency_key=f"hb-{uuid.uuid4()}").id)
        db.commit()
        asyncio.run(SocietyWorker(SessionLocal, settings=code_settings, model=FakeModel(_live_roles), worker_id="w-old", telemetry_enabled=False).run_until_idle(max_cycles=6))
    db.expire_all()
    runs = [r.id for r in db.query(AgentRun).filter(AgentRun.event_id.in_(beats)).all()]
    acted = [i.intent_type for i in db.query(AgentIntent).filter(AgentIntent.run_id.in_(runs)).all()]
    assert runs and acted and set(acted) == {"WRITE_MEMORY"}, "the deadlock: every run writes memory"
    assert _ev(db.get(CodeCandidate, cand.id).status) == "built" and not _qa_requests(db, cand)

    # ── the operator sees it, and its slot is free; nothing was abandoned ──
    queue = candidate_health.operator_queue(db, code_settings)
    stalled = {r["candidate_id"]: r for r in queue["stalled"]}
    assert str(cand.id) in stalled and stalled[str(cand.id)]["status"] == "built" and stalled[str(cand.id)]["hours_without_progress"] >= 24
    acct = portfolio_accounting(db, code_settings)
    assert str(proposal.id) in acct["stalled"] and str(proposal.id) not in acct["active"]

    # ── the fix: the worker's deterministic sweep clears A's verdict and asks QA for B ──
    monkeypatch.undo()
    asyncio.run(SocietyWorker(SessionLocal, settings=code_settings, model=FakeModel(_live_roles), worker_id="w-new", telemetry_enabled=False).run_until_idle(max_cycles=10))
    db.expire_all()
    (request,) = _qa_requests(db, cand)
    assert request.actor_type == "system" and int(request.causation_depth or 0) == 0 and request.correlation_id != cand.correlation_id
    assert request.payload["head_sha"] == head_b and request.payload["reason"] == "stale_verdict"
    assert [r.role for r in db.query(AgentRun).filter(AgentRun.event_id == request.id).all()] == ["qa"]
    evaluated = db.get(CodeCandidate, cand.id)
    assert evaluated.qa_report["verdict"] == "pass" and evaluated.qa_report["head_sha"] == head_b
    assert evaluated.qa_report["attempts"] == 2, "the QA attempt cap still counts the failed head"
    assert _ev(evaluated.status) == "ready", evaluated.status
    stalled_notice = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.CODE_CANDIDATE_STALLED, SocietyEvent.subject_id == cand.id).all()
    assert len(stalled_notice) == 1 and stalled_notice[0].payload["status"] == "built"
    assert db.query(AgentRun).filter(AgentRun.event_id == stalled_notice[0].id).count() == 0, "an operator notice wakes no role"
    assert str(cand.id) not in {r["candidate_id"] for r in candidate_health.operator_queue(db, code_settings)["stalled"]}

    # idempotent: another sweep neither re-requests QA nor re-notifies
    assert candidate_health.sweep(db, code_settings) == {"healed": 0, "stalled_notices": 0, "obsolete_notices": 0}


def test_a_resubmission_clears_the_old_verdict_and_requests_qa_for_the_new_head(db, SessionLocal, code_settings, grants_with_no_cooldown):
    report = _seed(db, grants_with_no_cooldown)
    _, cand = _proposal_and_candidate(db, report)
    cand = _built_then_failed(db, SessionLocal, code_settings, report, cand)
    head_a = cand.head_sha

    intent = _act(db, SessionLocal, code_settings, "builder", [_submit(cand, FULL_FIX, "accept 'on' and 'y' too")])
    assert _ev(intent.execution_status) == "executed"
    cand = db.get(CodeCandidate, cand.id)
    assert _ev(cand.status) == "built" and cand.head_sha != head_a
    assert "verdict" not in cand.qa_report and "summary" not in cand.qa_report and "failures" not in cand.qa_report
    assert cand.qa_report["attempts"] == 1 and cand.qa_report["previous"]["verdict"] == "fail" and cand.qa_report["previous"]["head_sha"] == head_a
    (request,) = _qa_requests(db, cand)
    assert request.payload["head_sha"] == cand.head_sha and request.payload["source_intent_id"] == str(intent.id)
    builder_story = db.get(AgentRun, intent.run_id).correlation_id
    assert int(request.causation_depth or 0) == 0 and request.correlation_id not in (builder_story, cand.correlation_id), "a fresh story"
    assert [r.role for r in db.query(AgentRun).filter(AgentRun.event_id == request.id).all()] == ["qa"], "QA is woken for the new head"

    assert _ev(_act(db, SessionLocal, code_settings, "qa", [_evaluate(cand.id)]).execution_status) == "executed"
    cand = db.get(CodeCandidate, cand.id)
    assert cand.qa_report["verdict"] == "pass" and cand.qa_report["head_sha"] == cand.head_sha and cand.qa_report["attempts"] == 2
    assert len(_qa_requests(db, cand)) == 1, "one QA request per head"


def test_a_stalled_candidate_reaches_the_operator_frees_its_slot_and_is_never_abandoned(db, society_settings, grants_with_no_cooldown):
    report = _seed(db, grants_with_no_cooldown)
    proposal, cand = _proposal_and_candidate(db, report)
    emit_event(db, event_type=EventType.CODE_CHANGE_REQUESTED, payload={"candidate_id": str(cand.id)}, actor_type="agent", actor_id=report.agents["architect"],
               subject_type="code_candidate", subject_id=cand.id, idempotency_key=f"t-req-{cand.id}")
    db.commit()
    assert str(proposal.id) in portfolio_accounting(db, society_settings)["active"], "fresh work holds its slot"
    assert candidate_health.operator_queue(db, society_settings)["stalled"] == []

    _backdate(db, cand, days=2)
    # a wake that only re-sends the request is not progress
    emit_event(db, event_type=EventType.CODE_CHANGE_REQUESTED, payload={"candidate_id": str(cand.id), "redelivered_from": str(uuid.uuid4())},
               subject_type="code_candidate", subject_id=cand.id, idempotency_key=f"t-redeliver-{cand.id}")
    db.commit()
    assert [r["candidate_id"] for r in candidate_health.operator_queue(db, society_settings)["stalled"]] == [str(cand.id)]
    assert str(proposal.id) in portfolio_accounting(db, society_settings)["stalled"]
    assert candidate_health.sweep(db, society_settings)["stalled_notices"] == 1
    assert candidate_health.sweep(db, society_settings)["stalled_notices"] == 0, "one notice per stalled episode"
    assert _ev(db.get(CodeCandidate, cand.id).status) == "requested", "only an operator abandons"

    # real progress makes it active again
    emit_event(db, event_type=EventType.CODE_CANDIDATE_BUILT, payload={"candidate_id": str(cand.id)}, actor_type="agent", actor_id=report.agents["builder"],
               subject_type="code_candidate", subject_id=cand.id, idempotency_key=f"t-built-{cand.id}")
    db.commit()
    assert candidate_health.operator_queue(db, society_settings)["stalled"] == []
    assert str(proposal.id) in portfolio_accounting(db, society_settings)["active"]


def test_a_candidate_whose_anomaly_recovered_is_obsolete_for_the_operator(db, society_settings, grants_with_no_cooldown, api_client, user_token):
    report = _seed(db, grants_with_no_cooldown)
    story = uuid.uuid4()
    anomaly = emit_event(db, event_type=EventType.PUBLIC_SURFACE_ANOMALY, payload={"source": "public_surface_monitor"}, correlation_id=story, idempotency_key=f"t-anomaly-{story}")
    db.commit()
    _, target = _proposal_and_candidate(db, report, status="built", correlation_id=story)
    _, unrelated = _proposal_and_candidate(db, report, status="built")
    assert candidate_health.operator_queue(db, society_settings)["obsolete"] == []

    emit_event(db, event_type=EventType.PUBLIC_SURFACE_RECOVERED, payload={"anomaly_event_id": str(anomaly.id)}, subject_type="society_event", subject_id=anomaly.id,
               correlation_id=story, idempotency_key=f"public-surface-recovered:{anomaly.id}")
    db.commit()
    (row,) = candidate_health.operator_queue(db, society_settings)["obsolete"]
    assert row["candidate_id"] == str(target.id) and row["reason"] == "target_recovered" and row["anomaly_event_id"] == str(anomaly.id)
    assert candidate_health.sweep(db, society_settings)["obsolete_notices"] == 1
    assert candidate_health.sweep(db, society_settings)["obsolete_notices"] == 0
    assert _ev(db.get(CodeCandidate, target.id).status) == "built" and _ev(db.get(CodeCandidate, unrelated.id).status) == "built"

    _, op_tok = user_token("operator")
    body = api_client.get("/v1/society/approvals", headers=auth(op_tok)).json()
    assert [r["candidate_id"] for r in body["candidates"]["obsolete"]] == [str(target.id)]
    assert "stalled" in body["candidates"]
