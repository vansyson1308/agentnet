"""Durable approval + resume lifecycle (Phase 2-B)."""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import timedelta

import pytest

from services.registry.app.models import AgentCapabilityGrant, AgentIntent, AgentRun, ImprovementProposal, IntentApproval, SocietyEvent
from services.registry.app.society import approvals as ap
from services.registry.app.society.cognition import FakeModel
from services.registry.app.society.config import SocietySettings, reset_settings_cache
from services.registry.app.society.events import EventType, emit_event, utcnow
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker

from .conftest import auth


def _ev(v):
    return v.value if hasattr(v, "value") else v


PROPOSAL_INTENT = {"type": "CREATE_IMPROVEMENT", "payload": {"title": "Gated proposal", "problem": "p", "proposed_change": "c", "importance": 80}}


def _park_intent(db, SessionLocal, settings, grants_with_no_cooldown, intent=PROPOSAL_INTENT, role="scout"):
    """Run the loop once with a grant that requires approval for the intent;
    returns the parked AgentIntent."""
    report = seed_society(db)
    grants_with_no_cooldown()
    g = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == report.agents[role]).first()
    g.approval_required_intents = [intent["type"]]
    db.commit()
    model = FakeModel({f"Society_{role.capitalize()}" if role != "qa" else "Society_QA": [{"decision_summary": "gated", "intents": [intent]}]})
    emit_event(db, event_type="t.gate")
    db.commit()
    worker = SocietyWorker(SessionLocal, settings=settings, model=model, worker_id="w")
    worker.routing = {"t.gate": [role]}
    asyncio.run(worker.run_until_idle(max_cycles=10))
    row = db.query(AgentIntent).filter(AgentIntent.intent_type == intent["type"]).first()
    assert row is not None and _ev(row.execution_status) == "awaiting_approval", (row.execution_status, row.policy_reason)
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.INTENT_APPROVAL_REQUIRED).count() == 1
    return row, model, worker


def test_approve_then_resume_executes_without_calling_model_again(db, SessionLocal, society_settings, grants_with_no_cooldown, make_user):
    intent, model, worker = _park_intent(db, SessionLocal, society_settings, grants_with_no_cooldown)
    assert db.query(ImprovementProposal).count() == 0
    op = make_user("op@test")
    res = ap.decide(db, intent_id=intent.id, user=op, decision="approved", reason="looks safe")
    assert not res.already_decided and _ev(res.approval.decision) == "approved"
    db.refresh(intent)
    assert _ev(intent.execution_status) == "approved"
    calls_before = len(model.calls)
    stats = asyncio.run(worker.run_until_idle(max_cycles=5))
    assert stats.approved_intents_resumed == 1
    assert len(model.calls) == calls_before, "resume must not call the model"
    db.expire_all()
    intent = db.query(AgentIntent).filter(AgentIntent.id == intent.id).first()
    assert _ev(intent.execution_status) == "executed" and intent.result["resumed_by"] == "w"
    assert db.query(ImprovementProposal).count() == 1
    approval = db.query(IntentApproval).filter(IntentApproval.intent_id == intent.id).first()
    assert approval.final_state == "executed" and approval.resumed_at and approval.executed_at and approval.decided_by_user_id == op.id
    assert approval.original_policy_reason and "requires human approval" in approval.original_policy_reason
    types = [e.event_type for e in db.query(SocietyEvent).order_by(SocietyEvent.created_at).all()]
    for t in (EventType.INTENT_APPROVAL_REQUIRED, EventType.INTENT_APPROVED, EventType.INTENT_RESUMED, EventType.INTENT_EXECUTED, EventType.PROPOSAL_CREATED):
        assert t in types, types
    # follow-up proposal event carries the original correlation
    run = db.query(AgentRun).filter(AgentRun.id == intent.run_id).first()
    prop_ev = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.PROPOSAL_CREATED).first()
    assert prop_ev.correlation_id == run.correlation_id and prop_ev.source_run_id == run.id
    # idempotent re-approval by the same decision is harmless
    again = ap.decide(db, intent_id=intent.id, user=op, decision="approved")
    assert again.already_decided


def test_reject_never_executes_and_approve_after_reject_is_refused(db, SessionLocal, society_settings, grants_with_no_cooldown, make_user):
    intent, model, worker = _park_intent(db, SessionLocal, society_settings, grants_with_no_cooldown)
    op = make_user("op@test")
    ap.decide(db, intent_id=intent.id, user=op, decision="rejected", reason="not now")
    db.refresh(intent)
    assert _ev(intent.execution_status) == "rejected"
    with pytest.raises(ap.ApprovalConflict):
        ap.decide(db, intent_id=intent.id, user=make_user("op2@test"), decision="approved")
    asyncio.run(worker.run_until_idle(max_cycles=5))
    assert db.query(ImprovementProposal).count() == 0
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.INTENT_REJECTED).count() == 1
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.INTENT_RESUMED).count() == 0


def test_reject_after_approve_and_double_decision_conflicts(db, SessionLocal, society_settings, grants_with_no_cooldown, make_user):
    intent, model, worker = _park_intent(db, SessionLocal, society_settings, grants_with_no_cooldown)
    ap.decide(db, intent_id=intent.id, user=make_user("a@test"), decision="approved")
    with pytest.raises(ap.ApprovalConflict):
        ap.decide(db, intent_id=intent.id, user=make_user("b@test"), decision="rejected")
    with pytest.raises(ap.ApprovalNotFound):
        ap.decide(db, intent_id=uuid.uuid4(), user=make_user("c@test"), decision="approved")


def test_two_operators_racing_exactly_one_decision_wins(db, SessionLocal, society_settings, grants_with_no_cooldown, make_user):
    intent, model, worker = _park_intent(db, SessionLocal, society_settings, grants_with_no_cooldown)
    users = [make_user(f"race{i}@test") for i in range(6)]
    results = []
    barrier = threading.Barrier(6)

    def go(i, decision):
        s = SessionLocal()
        try:
            barrier.wait()
            try:
                r = ap.decide(s, intent_id=intent.id, user=s.merge(users[i]), decision=decision)
                results.append(("ok", decision, r.already_decided))
            except ap.ApprovalConflict:
                results.append(("conflict", decision, None))
        finally:
            s.close()

    ts = [threading.Thread(target=go, args=(i, "approved" if i % 2 == 0 else "rejected")) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert len(results) == 6
    assert db.query(IntentApproval).filter(IntentApproval.intent_id == intent.id).count() == 1
    winner = db.query(IntentApproval).filter(IntentApproval.intent_id == intent.id).first()
    oks = [r for r in results if r[0] == "ok"]
    assert all(r[1] == _ev(winner.decision) for r in oks), results
    assert sum(1 for r in oks if r[2] is False) == 1, results  # exactly one first decision
    assert all(r[1] != _ev(winner.decision) for r in results if r[0] == "conflict")


def test_resume_recheck_fails_closed_when_flag_or_grant_changed(db, SessionLocal, society_settings, grants_with_no_cooldown, make_user, monkeypatch):
    code_intent = {"type": "REQUEST_CODE_CHANGE", "payload": {"title": "gated change", "spec": {"description": "d", "files_allowed": ["docs/society/candidates/x.md"], "acceptance_tests": ["tests/society/acceptance/test_candidate_docs.py"]}}}
    intent, model, worker = _park_intent(db, SessionLocal, society_settings, grants_with_no_cooldown, intent=code_intent, role="architect")
    ap.decide(db, intent_id=intent.id, user=make_user("op@test"), decision="approved")
    # the world changed while approval was pending: autonomous code got disabled
    monkeypatch.setenv("SOCIETY_AUTONOMOUS_CODE_ENABLED", "false")
    reset_settings_cache()
    worker.settings = SocietySettings()
    asyncio.run(worker.run_until_idle(max_cycles=5))
    db.expire_all()
    intent = db.query(AgentIntent).filter(AgentIntent.id == intent.id).first()
    assert _ev(intent.execution_status) == "denied" and "SOCIETY_AUTONOMOUS_CODE_ENABLED" in intent.error
    from services.registry.app.models import CodeCandidate

    assert db.query(CodeCandidate).count() == 0
    approval = db.query(IntentApproval).filter(IntentApproval.intent_id == intent.id).first()
    assert approval.final_state == "denied"
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.INTENT_DENIED).count() >= 1


def test_resume_recheck_fails_closed_when_grant_disabled(db, SessionLocal, society_settings, grants_with_no_cooldown, make_user):
    intent, model, worker = _park_intent(db, SessionLocal, society_settings, grants_with_no_cooldown)
    ap.decide(db, intent_id=intent.id, user=make_user("op@test"), decision="approved")
    g = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == intent.agent_id).first()
    g.enabled = False
    db.commit()
    asyncio.run(worker.run_until_idle(max_cycles=5))
    db.expire_all()
    intent = db.query(AgentIntent).filter(AgentIntent.id == intent.id).first()
    assert _ev(intent.execution_status) == "denied" and "disabled" in intent.error
    assert db.query(ImprovementProposal).count() == 0


def test_forbidden_high_intents_can_never_be_approved(db, SessionLocal, society_settings, grants_with_no_cooldown, make_user):
    """Even if a row somehow sits in awaiting_approval with a HIGH type, approving it is refused
    at decision time AND at resume time."""
    report = seed_society(db)
    grants_with_no_cooldown()
    emit_event(db, event_type="t.x")
    db.commit()
    from services.registry.app.society.runs import claim_next_run, dispatch_pending_events

    dispatch_pending_events(db, settings=society_settings, routing={"t.x": ["scout"]})
    run = claim_next_run(db, worker_id="w", lease_seconds=60)
    bad = AgentIntent(id=uuid.uuid4(), run_id=run.id, agent_id=run.agent_id, seq=0, intent_type="SHELL_EXEC", payload={"cmd": "rm -rf /"}, idempotency_key=f"{run.id}:forced", risk_class="high", policy_decision="approval_required", policy_reason="forced by test", execution_status="awaiting_approval")
    db.add(bad)
    db.commit()
    with pytest.raises(ap.ApprovalConflict):
        ap.decide(db, intent_id=bad.id, user=make_user("op@test"), decision="approved")
    # force the state anyway to prove resume also refuses
    bad.execution_status = "approved"
    db.commit()
    from services.registry.app.society.approvals import claim_next_approved_intent, execute_approved_intent

    claimed = claim_next_approved_intent(db, worker_id="w", lease_seconds=60)
    assert claimed.id == bad.id
    assert execute_approved_intent(db, claimed, settings=society_settings, worker_id="w") == "denied"
    db.refresh(bad)
    assert "fail closed" in bad.error
    assert report.agents


def test_resume_lease_crash_recovery_and_bounded_attempts(db, SessionLocal, society_settings, grants_with_no_cooldown, make_user, monkeypatch):
    intent, model, worker = _park_intent(db, SessionLocal, society_settings, grants_with_no_cooldown)
    ap.decide(db, intent_id=intent.id, user=make_user("op@test"), decision="approved")
    from services.registry.app.society.approvals import claim_next_approved_intent

    s1 = SessionLocal()
    claimed = claim_next_approved_intent(s1, worker_id="crashed", lease_seconds=60)
    assert claimed.id == intent.id and claimed.resume_attempt == 1
    s2 = SessionLocal()
    assert claim_next_approved_intent(s2, worker_id="other", lease_seconds=60) is None  # lease held
    claimed.resume_lease_expires_at = utcnow() - timedelta(seconds=1)
    s1.commit()
    re = claim_next_approved_intent(s2, worker_id="other", lease_seconds=60)
    assert re.id == intent.id and re.resume_attempt == 2
    s1.close()
    s2.close()
    # exhaust attempts: a worker that keeps dying before completing
    monkeypatch.setenv("SOCIETY_APPROVAL_RESUME_MAX_ATTEMPTS", "2")
    reset_settings_cache()
    worker.settings = SocietySettings()
    intent = db.query(AgentIntent).filter(AgentIntent.id == intent.id).first()
    intent.resume_lease_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    asyncio.run(worker.run_until_idle(max_cycles=5))
    db.expire_all()
    intent = db.query(AgentIntent).filter(AgentIntent.id == intent.id).first()
    assert _ev(intent.execution_status) == "failed" and "resume attempts exhausted" in intent.error
    assert db.query(ImprovementProposal).count() == 0


def test_resume_is_deferred_while_runtime_disabled(db, SessionLocal, society_settings, grants_with_no_cooldown, make_user, monkeypatch):
    intent, model, worker = _park_intent(db, SessionLocal, society_settings, grants_with_no_cooldown)
    ap.decide(db, intent_id=intent.id, user=make_user("op@test"), decision="approved")
    monkeypatch.setenv("SOCIETY_RUNTIME_ENABLED", "false")
    reset_settings_cache()
    from services.registry.app.society.approvals import claim_next_approved_intent, execute_approved_intent

    claimed = claim_next_approved_intent(db, worker_id="w", lease_seconds=60)
    assert execute_approved_intent(db, claimed, settings=SocietySettings(), worker_id="w") == "approved"
    db.refresh(claimed)
    assert claimed.resume_attempt == 0 and claimed.resume_lease_expires_at is None


def test_approval_api_end_to_end(api_client, db, SessionLocal, society_settings, grants_with_no_cooldown, user_token):
    intent, model, worker = _park_intent(db, SessionLocal, society_settings, grants_with_no_cooldown)
    _, user_tok = user_token(None)
    _, op_tok = user_token("operator")
    assert api_client.post(f"/v1/society/intents/{intent.id}/approve", headers=auth(user_tok), json={"reason": "x"}).status_code == 403
    listing = api_client.get("/v1/society/approvals", headers=auth(op_tok)).json()
    assert [i["id"] for i in listing["pending"]] == [str(intent.id)]
    r = api_client.post(f"/v1/society/intents/{intent.id}/approve", headers=auth(op_tok), json={"reason": "go"})
    assert r.status_code == 200 and r.json()["approval"]["decision"] == "approved" and r.json()["already_decided"] is False
    r2 = api_client.post(f"/v1/society/intents/{intent.id}/approve", headers=auth(op_tok), json={"reason": "go again"})
    assert r2.status_code == 200 and r2.json()["already_decided"] is True
    assert api_client.post(f"/v1/society/intents/{intent.id}/reject", headers=auth(op_tok), json={}).status_code == 409
    assert api_client.post(f"/v1/society/intents/{uuid.uuid4()}/approve", headers=auth(op_tok), json={}).status_code == 404
    listing = api_client.get("/v1/society/approvals", headers=auth(op_tok), params={"include_decided": "true"}).json()
    assert listing["approved_waiting_resume"] and listing["decided"][0]["decision"] == "approved"
    asyncio.run(worker.run_until_idle(max_cycles=5))
    listing = api_client.get("/v1/society/approvals", headers=auth(op_tok), params={"include_decided": "true"}).json()
    assert listing["pending"] == [] and listing["approved_waiting_resume"] == [] and listing["decided"][0]["final_state"] == "executed"
    body = api_client.get("/v1/society/ask", headers=auth(op_tok), params={"q": "what is awaiting approval"}).json()
    assert body["answers"]["blocked"]["intents_awaiting_human_approval"] == []
