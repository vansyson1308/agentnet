"""A Builder may decline a candidate it cannot finish within its spec.

Staging 2026-09-27 03:00Z: candidate a2788678 (proposal ea350455) failed QA
because active templates call ``url_for`` on endpoints that live in
``base.html`` -- a file outside the spec's ``files_allowed``. The Builder
correctly refused to widen scope and slept every hour; the Architect could not
re-specify (a request for the same proposal is a duplicate while the
candidate is open); QA rejects only on a second failure, which needs a
resubmission; and ABANDON is an operator act. Nothing could reach the designed
recovery: REJECTED -> proposal concluded -> the Scout re-proposes -> the
Governor approves -> the Architect designs the next candidate.

DECLINE_CODE_CANDIDATE is that missing exit, and nothing more: the
responsible Builder, a requested or qa_failed candidate, a structured reason
whose blocking paths really are outside ``files_allowed``; the outcome is the
ordinary REJECTED (spec never widened), idempotent, auditable, with the
implementation task closed through the ordinary escrow path.
"""

from __future__ import annotations

import asyncio
import uuid

from services.registry.app.models import (
    AgentIntent,
    CodeCandidate,
    CodePromotion,
    ImprovementProposal,
    SocietyEvent,
    TaskSession,
    Wallet,
    WalletOwnerType,
)
from services.registry.app.society.cognition import FakeModel
from services.registry.app.society.company import portfolio_accounting
from services.registry.app.society.events import EventType, emit_event
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker

MAIN = "services/dashboard/app/main.py"
BASE = "services/dashboard/app/templates/base.html"
FILES_ALLOWED = [
    MAIN,
    "services/dashboard/app/templates/landing.html",
    "services/dashboard/app/templates/metaverse.html",
    "services/dashboard/app/templates/network.html",
]
QA_FAILURE = (
    "services/dashboard/tests/test_public_surface.py::test_every_active_template_url_for_names_a_route: "
    "base.html url_for directory_page, wallet_page, tasks_page (no such endpoint)"
)
DETAIL = "QA fails on url_for endpoints referenced by base.html, which is outside files_allowed; the spec cannot pass as written."


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _seed(db, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    return report


def _live_deadlock(db, report, *, status="qa_failed", builder="builder"):
    """The staging rows as they stood at 03:00Z: an approved proposal whose one
    candidate failed QA on a file outside its spec."""
    proposal = ImprovementProposal(
        id=uuid.uuid4(),
        proposed_by_agent_id=report.agents["scout"],
        source="audit",
        title="Production public surface: 6/17 checks failing",
        problem="login/register/marketplace masked by landing redirect",
        proposed_change="add the public routes",
        status="CONVERTED_TO_TASK",
        target_scope="platform",
        importance=80,
    )
    db.add(proposal)
    db.flush()
    cand = CodeCandidate(
        id=uuid.uuid4(),
        proposal_id=proposal.id,
        correlation_id=uuid.uuid4(),
        requested_by_agent_id=report.agents["architect"],
        builder_agent_id=report.agents[builder] if builder else None,
        title="Public surface: add /login, /register, /marketplace routes",
        spec={
            "kind": "code",
            "description": "add the three public routes and de-placeholder the public templates",
            "expected_effect": "public surface contract checks pass",
            "files_allowed": list(FILES_ALLOWED),
            "acceptance_tests": ["services/dashboard/tests/test_public_surface.py"],
            "must_compile": True,
        },
        status=status,
        qa_report={"verdict": "fail", "attempts": 1, "failures": [QA_FAILURE]} if status == "qa_failed" else {},
        risk_tier="amber",
        changed_files=[MAIN] if status != "requested" else [],
    )
    db.add(cand)
    db.commit()
    return proposal, cand


def _decline(cid, *, reason="spec_outside_files_allowed", blocking=(BASE,), detail=DETAIL):
    return {"type": "DECLINE_CODE_CANDIDATE", "payload": {"candidate_id": str(cid), "reason_code": reason, "detail": detail, "blocking_paths": list(blocking)}}


def _run_as(db, SessionLocal, settings, role, intents, *, max_cycles=6):
    """One decision by ``role`` on a test event routed only to it."""
    ev = emit_event(db, event_type="t.decline", payload={}, correlation_id=uuid.uuid4(), idempotency_key=f"t-decline-{uuid.uuid4()}")
    db.commit()
    worker = SocietyWorker(SessionLocal, settings=settings, model=FakeModel({role: [{"decision_summary": "act", "intents": intents, "sleep_for_seconds": 1}]}), worker_id="w-decline", telemetry_enabled=False)
    worker.routing = {"t.decline": [role]}
    asyncio.run(worker.run_until_idle(max_cycles=max_cycles))
    db.expire_all()
    return ev


def _intent(db, itype):
    return db.query(AgentIntent).filter(AgentIntent.intent_type == itype).order_by(AgentIntent.created_at.desc()).first()


def test_the_live_deadlock_is_released_by_the_builder_and_recovery_designs_the_next_candidate(db, SessionLocal, code_settings, grants_with_no_cooldown):
    report = _seed(db, grants_with_no_cooldown)
    proposal, cand = _live_deadlock(db, report)

    # ── the deadlock, exactly as observed ──
    same_spec = {"title": cand.title, "proposal_id": str(proposal.id), "requires_security_review": True, "spec": dict(cand.spec)}
    _run_as(db, SessionLocal, code_settings, "architect", [{"type": "REQUEST_CODE_CHANGE", "payload": same_spec}])
    assert _intent(db, "REQUEST_CODE_CHANGE").result["result"]["duplicate"] is True, "the Architect cannot re-specify an open candidate"
    _run_as(db, SessionLocal, code_settings, "builder", [{"type": "REQUEST_QA", "payload": {"candidate_id": str(cand.id)}}])
    assert _ev(_intent(db, "REQUEST_QA").execution_status) == "failed", "QA cannot be re-run on a qa_failed candidate"
    assert db.query(CodeCandidate).count() == 1 and _ev(db.get(CodeCandidate, cand.id).status) == "qa_failed"

    # ── the Builder declines; the Society recovers on its own ──
    new_spec = {
        "kind": "code",
        "description": "second design: include the template the acceptance test needs",
        "expected_effect": "public surface contract checks pass",
        "files_allowed": FILES_ALLOWED + [BASE],
        "acceptance_tests": ["services/dashboard/tests/test_public_surface.py"],
    }

    def society(ctx):
        et, role, p = ctx.event["type"], ctx.role, ctx.event["payload"]["data"]
        if role == "builder" and et == EventType.SOCIETY_HEARTBEAT:
            return {"decision_summary": "cannot finish within the spec; declining", "intents": [_decline(cand.id)], "sleep_for_seconds": 60}
        if role == "scout" and et == EventType.CODE_CANDIDATE_REJECTED:
            assert p["declined"] is True and p["reason_code"] == "spec_outside_files_allowed"
            return {"decision_summary": "re-propose with the lesson", "intents": [{"type": "CREATE_IMPROVEMENT", "payload": {
                "title": "Public surface repair, second design", "problem": "candidate declined: " + p["detail"][:200],
                "proposed_change": "re-design including base.html", "importance": 80}}], "sleep_for_seconds": 60}
        if role == "governor" and et == EventType.PROPOSAL_CREATED:
            return {"decision_summary": "approve", "intents": [{"type": "REVIEW_IMPROVEMENT", "payload": {"proposal_id": p["proposal_id"], "decision": "approve", "reason": "bounded re-design"}}], "sleep_for_seconds": 60}
        if role == "architect" and et == EventType.PROPOSAL_APPROVED:
            return {"decision_summary": "design candidate 2", "intents": [{"type": "REQUEST_CODE_CHANGE", "payload": {
                "title": "Public surface repair, second design", "proposal_id": p["proposal_id"], "requires_security_review": True, "spec": new_spec}}], "sleep_for_seconds": 60}
        return {"decision_summary": "nothing to do", "intents": [], "sleep_for_seconds": 60}

    emit_event(db, event_type=EventType.SOCIETY_HEARTBEAT, payload={}, correlation_id=uuid.uuid4(), idempotency_key=f"hb-{uuid.uuid4()}")
    db.commit()
    asyncio.run(SocietyWorker(SessionLocal, settings=code_settings, model=FakeModel(society), worker_id="w-recover", telemetry_enabled=False).run_until_idle(max_cycles=30))
    db.expire_all()

    declined = db.get(CodeCandidate, cand.id)
    assert _ev(declined.status) == "rejected", declined.error
    assert declined.spec["files_allowed"] == FILES_ALLOWED, "a decline never widens the spec"
    assert declined.qa_report["failures"] == [QA_FAILURE], "the evidence of the failure is kept"
    assert declined.error.startswith("declined by Society_Builder (spec_outside_files_allowed)")
    intent = _intent(db, "DECLINE_CODE_CANDIDATE")
    assert _ev(intent.execution_status) == "executed"
    assert intent.payload["reason_code"] == "spec_outside_files_allowed" and intent.payload["blocking_paths"] == [BASE]
    rejected = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.CODE_CANDIDATE_REJECTED, SocietyEvent.subject_id == cand.id).one()
    assert rejected.payload["declined"] is True and rejected.payload["blocking_paths"] == [BASE] and rejected.actor_id == report.agents["builder"]

    assert str(proposal.id) in {str(x) for x in portfolio_accounting(db, code_settings)["concluded"]}, "the old hypothesis no longer holds a slot"
    new = db.query(CodeCandidate).filter(CodeCandidate.id != cand.id).one()
    assert _ev(new.status) == "requested" and new.proposal_id != proposal.id and BASE in new.spec["files_allowed"]


def test_only_the_responsible_builder_may_decline_an_open_candidate(db, SessionLocal, code_settings, grants_with_no_cooldown):
    report = _seed(db, grants_with_no_cooldown)

    _, other = _live_deadlock(db, report, builder="qa")  # built by someone else
    _run_as(db, SessionLocal, code_settings, "builder", [_decline(other.id)])
    assert _ev(_intent(db, "DECLINE_CODE_CANDIDATE").execution_status) == "failed"
    assert "responsible" in _intent(db, "DECLINE_CODE_CANDIDATE").error

    _, mine = _live_deadlock(db, report)
    _run_as(db, SessionLocal, code_settings, "architect", [_decline(mine.id)])
    assert _ev(_intent(db, "DECLINE_CODE_CANDIDATE").policy_decision) == "deny", "only the Builder's grant carries the intent"

    for status in ("ready", "qa_passed", "security_review", "qa_running", "built", "building", "rejected", "abandoned", "failed"):
        _, c = _live_deadlock(db, report, status=status)
        _run_as(db, SessionLocal, code_settings, "builder", [_decline(c.id)])
        i = _intent(db, "DECLINE_CODE_CANDIDATE")
        assert _ev(i.execution_status) == "failed" and status in i.error, (status, i.error)
        assert _ev(db.get(CodeCandidate, c.id).status) == status

    _, promoted = _live_deadlock(db, report)
    db.add(CodePromotion(candidate_id=promoted.id, correlation_id=uuid.uuid4(), risk_tier="amber", provider="fake", status="requested"))
    db.commit()
    _run_as(db, SessionLocal, code_settings, "builder", [_decline(promoted.id)])
    assert "promotion" in _intent(db, "DECLINE_CODE_CANDIDATE").error

    _, c = _live_deadlock(db, report)
    _run_as(db, SessionLocal, code_settings, "builder", [_decline(c.id, blocking=(MAIN,))])
    assert "inside files_allowed" in _intent(db, "DECLINE_CODE_CANDIDATE").error, "a decline must be true, not convenient"
    _run_as(db, SessionLocal, code_settings, "builder", [_decline(c.id, blocking=())])
    assert "blocking_paths" in _intent(db, "DECLINE_CODE_CANDIDATE").error
    _run_as(db, SessionLocal, code_settings, "builder", [_decline(c.id, detail="too short")])
    assert _ev(_intent(db, "DECLINE_CODE_CANDIDATE").policy_decision) == "invalid", "the structured reason is required"
    assert _ev(db.get(CodeCandidate, c.id).status) == "qa_failed" and db.get(CodeCandidate, c.id).spec["files_allowed"] == FILES_ALLOWED


def test_a_decline_is_idempotent_and_refunds_the_implementation_task_once(db, SessionLocal, code_settings, grants_with_no_cooldown):
    report = _seed(db, grants_with_no_cooldown)
    caller = db.query(Wallet).filter(Wallet.owner_type == WalletOwnerType.AGENT, Wallet.owner_id == report.agents["architect"]).first()
    caller.balance_credits = 50
    db.commit()
    _run_as(db, SessionLocal, code_settings, "architect", [{"type": "CREATE_TASK", "payload": {"callee_agent": "Society_Builder", "capability": "implement_change", "input": {"candidate_id": "x"}, "max_budget": 10}}])
    task = db.query(TaskSession).one()
    caller = db.query(Wallet).filter(Wallet.id == caller.id).one()
    assert caller.reserved_credits == 10

    _, cand = _live_deadlock(db, report, status="requested", builder=None)  # never picked up: any Builder holds it
    cand.task_id = task.id
    db.commit()
    _run_as(db, SessionLocal, code_settings, "builder", [_decline(cand.id, reason="acceptance_unsatisfiable", blocking=())])
    first = _intent(db, "DECLINE_CODE_CANDIDATE")
    assert _ev(first.execution_status) == "executed" and first.result["result"]["task_refunded"] is True
    assert _ev(db.get(TaskSession, task.id).status) == "failed"
    caller = db.query(Wallet).filter(Wallet.id == caller.id).one()
    assert caller.balance_credits == 50 and caller.reserved_credits == 0, "the escrow is released exactly once"

    _run_as(db, SessionLocal, code_settings, "builder", [_decline(cand.id, reason="acceptance_unsatisfiable", blocking=())])
    again = _intent(db, "DECLINE_CODE_CANDIDATE")
    assert _ev(again.execution_status) == "executed" and again.result["result"]["duplicate"] is True
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.CODE_CANDIDATE_REJECTED, SocietyEvent.subject_id == cand.id).count() == 1
    caller = db.query(Wallet).filter(Wallet.id == caller.id).one()
    assert caller.balance_credits == 50 and caller.reserved_credits == 0
