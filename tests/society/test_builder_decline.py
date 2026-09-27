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
    AgentCapabilityGrant,
    AgentIntent,
    AgentRun,
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


def _intent_of(db, ev):
    """The intent the run for ``ev`` produced -- never an older one, so a run the
    worker skipped fails loudly instead of passing on a previous result."""
    run = db.query(AgentRun).filter(AgentRun.event_id == ev.id).one()
    return db.query(AgentIntent).filter(AgentIntent.run_id == run.id).one()


def _intent(db, itype, cid=None):
    """The newest intent of ``itype`` -- for ``cid``, the newest about that candidate."""
    rows = db.query(AgentIntent).filter(AgentIntent.intent_type == itype).order_by(AgentIntent.created_at.desc()).all()
    return next((r for r in rows if cid is None or (r.payload or {}).get("candidate_id") == str(cid)), None)


def test_the_live_deadlock_is_released_by_the_builder_and_recovery_designs_the_next_candidate(db, SessionLocal, code_settings, grants_with_no_cooldown):
    report = _seed(db, grants_with_no_cooldown)
    proposal, cand = _live_deadlock(db, report)

    # ── the deadlock, exactly as observed ──
    same_spec = {"title": cand.title, "proposal_id": str(proposal.id), "requires_security_review": True, "spec": dict(cand.spec)}
    _run_as(db, SessionLocal, code_settings, "architect", [{"type": "REQUEST_CODE_CHANGE", "payload": same_spec}])
    assert _intent(db, "REQUEST_CODE_CHANGE").result["result"]["duplicate"] is True, "the Architect cannot re-specify an open candidate"
    _run_as(db, SessionLocal, code_settings, "builder", [{"type": "REQUEST_QA", "payload": {"candidate_id": str(cand.id)}}])
    assert _ev(_intent(db, "REQUEST_QA").execution_status) == "failed", "QA cannot be re-run on a qa_failed candidate"
    same_title = {"type": "CREATE_IMPROVEMENT", "payload": {"title": proposal.title, "problem": "public surface failing", "proposed_change": "repair it", "importance": 80}}
    _run_as(db, SessionLocal, code_settings, "scout", [same_title])
    assert _intent(db, "CREATE_IMPROVEMENT").result["result"]["duplicate"] is True, "an attempt in flight is not re-proposed"
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
            # Same title as the concluded hypothesis: every attempt at it failed,
            # so re-proposing it is recovery, not a duplicate.
            return {"decision_summary": "re-propose with the lesson", "intents": [{"type": "CREATE_IMPROVEMENT", "payload": {
                "title": proposal.title, "problem": "candidate declined: " + p["detail"][:200],
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
    reproposed = db.get(ImprovementProposal, new.proposal_id)
    assert reproposed.title == proposal.title and _ev(reproposed.status) == "CONVERTED_TO_TASK", "re-reviewed by the Governor"


def test_only_the_responsible_builder_may_decline_an_open_candidate(db, SessionLocal, code_settings, grants_with_no_cooldown):
    report = _seed(db, grants_with_no_cooldown)
    db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == report.agents["builder"]).update({"max_runs_per_hour": 100})
    db.commit()

    _, other = _live_deadlock(db, report, builder="qa")  # built by someone else
    i = _intent_of(db, _run_as(db, SessionLocal, code_settings, "builder", [_decline(other.id)]))
    assert _ev(i.execution_status) == "failed" and "responsible" in i.error

    _, mine = _live_deadlock(db, report)
    i = _intent_of(db, _run_as(db, SessionLocal, code_settings, "architect", [_decline(mine.id)]))
    assert _ev(i.policy_decision) == "deny", "only the Builder's grant carries the intent"

    for status in ("ready", "qa_passed", "security_review", "qa_running", "built", "building", "rejected", "abandoned", "failed"):
        _, c = _live_deadlock(db, report, status=status)
        i = _intent_of(db, _run_as(db, SessionLocal, code_settings, "builder", [_decline(c.id)]))
        assert _ev(i.execution_status) == "failed" and f"candidate is {status};" in i.error, (status, i.error)
        assert _ev(db.get(CodeCandidate, c.id).status) == status

    _, promoted = _live_deadlock(db, report)
    db.add(CodePromotion(candidate_id=promoted.id, correlation_id=uuid.uuid4(), risk_tier="amber", provider="fake", status="requested"))
    db.commit()
    assert "promotion" in _intent_of(db, _run_as(db, SessionLocal, code_settings, "builder", [_decline(promoted.id)])).error

    _, fresh = _live_deadlock(db, report, status="requested")
    i = _intent_of(db, _run_as(db, SessionLocal, code_settings, "builder", [_decline(fresh.id, reason="acceptance_unsatisfiable", blocking=())]))
    assert "needs a qa_failed candidate" in i.error, "no QA failure, no claim that QA cannot pass"

    _, c = _live_deadlock(db, report)
    for blocking, refusal in (
        ((MAIN,), "inside files_allowed"),  # a decline must be true, not convenient
        ((BASE, MAIN), "inside files_allowed"),  # one real blocker does not excuse the rest
        ((), "blocking_paths"),
    ):
        i = _intent_of(db, _run_as(db, SessionLocal, code_settings, "builder", [_decline(c.id, blocking=blocking)]))
        assert _ev(i.execution_status) == "failed" and refusal in i.error, blocking
    for payload in (
        _decline(c.id, detail="too short"),
        _decline(c.id, blocking=("./" + BASE,)),
        _decline(c.id, blocking=("services//dashboard/app/templates/base.html",)),
        _decline(c.id, blocking=("services/dashboard/app/templates/base\n.html",)),
    ):
        i = _intent_of(db, _run_as(db, SessionLocal, code_settings, "builder", [payload]))
        assert _ev(i.policy_decision) == "invalid", payload
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
    first = _intent_of(db, _run_as(db, SessionLocal, code_settings, "builder", [_decline(cand.id)]))
    assert _ev(first.execution_status) == "executed" and first.result["result"]["task_refunded"] is True
    assert _ev(db.get(TaskSession, task.id).status) == "failed"
    caller = db.query(Wallet).filter(Wallet.id == caller.id).one()
    assert caller.balance_credits == 50 and caller.reserved_credits == 0, "the escrow is released exactly once"

    again = _intent_of(db, _run_as(db, SessionLocal, code_settings, "builder", [_decline(cand.id)]))
    assert _ev(again.execution_status) == "executed" and again.result["result"]["duplicate"] is True
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.CODE_CANDIDATE_REJECTED, SocietyEvent.subject_id == cand.id).count() == 1
    caller = db.query(Wallet).filter(Wallet.id == caller.id).one()
    assert caller.balance_credits == 50 and caller.reserved_credits == 0


def test_a_decline_never_closes_a_task_the_builder_is_not_a_party_to(db, SessionLocal, code_settings, grants_with_no_cooldown):
    """The candidate's task id comes from the Architect's request. Declining is
    the Builder's FAIL_TASK authority and no more: a task whose callee is
    someone else stays open for its own parties (and the timeout worker)."""
    report = _seed(db, grants_with_no_cooldown)
    caller = db.query(Wallet).filter(Wallet.owner_type == WalletOwnerType.AGENT, Wallet.owner_id == report.agents["architect"]).first()
    caller.balance_credits = 50
    db.commit()
    _run_as(db, SessionLocal, code_settings, "architect", [{"type": "CREATE_TASK", "payload": {"callee_agent": "Society_QA", "capability": "evaluate_candidate", "input": {"candidate_id": "x"}, "max_budget": 10}}])
    foreign = db.query(TaskSession).one()
    reserved = db.query(Wallet).filter(Wallet.id == caller.id).one().reserved_credits
    assert reserved > 0

    _, cand = _live_deadlock(db, report)
    cand.task_id = foreign.id
    db.commit()
    i = _intent_of(db, _run_as(db, SessionLocal, code_settings, "builder", [_decline(cand.id)]))
    assert _ev(i.execution_status) == "executed" and i.result["result"]["task_refunded"] is False
    assert _ev(db.get(CodeCandidate, cand.id).status) == "rejected"
    assert _ev(db.get(TaskSession, foreign.id).status) == "initiated", "someone else's task is never failed by a decline"
    assert db.query(Wallet).filter(Wallet.id == caller.id).one().reserved_credits == reserved


def test_the_decline_moves_money_only_through_the_escrow_path():
    """Never a wallet write: like the operator abandon, the refund is
    fail_task_with_refund's, for the callee's own task, once."""
    import inspect

    from services.registry.app.society import executor

    src = inspect.getsource(executor._decline_code_candidate)
    assert "task_service.fail_task_with_refund" in src and "callee_agent_id=ctx.agent.id" in src
    for forbidden in ("balance_credits", "reserved_credits", "Wallet", "wallet."):
        assert forbidden not in src, f"a decline must never move money itself: {forbidden!r}"
    assert "TaskStatus.INITIATED.value, TaskStatus.IN_PROGRESS.value" in src


def test_a_hypothesis_that_keeps_failing_is_not_re_proposed_forever(db, SessionLocal, code_settings, grants_with_no_cooldown):
    """Re-proposing a concluded title is recovery, bounded: after two failed
    attempts in 24h the Scout must change the approach, so an attempt that is
    rejected at once cannot loop until the daily candidate budget is spent."""
    report = _seed(db, grants_with_no_cooldown)
    first, c1 = _live_deadlock(db, report)
    c1.status = "rejected"
    db.commit()
    again = {"type": "CREATE_IMPROVEMENT", "payload": {"title": first.title, "problem": "still failing", "proposed_change": "try again", "importance": 80}}

    i = _intent_of(db, _run_as(db, SessionLocal, code_settings, "scout", [again]))
    assert _ev(i.execution_status) == "executed" and "duplicate" not in i.result["result"], "one failed attempt: recovery may re-propose"
    second = db.get(ImprovementProposal, uuid.UUID(i.result["result"]["proposal_id"]))
    i = _intent_of(db, _run_as(db, SessionLocal, code_settings, "scout", [again]))
    assert i.result["result"]["duplicate"] is True and i.result["result"]["proposal_id"] == str(second.id), "the new proposal is in flight"

    second.status = "CONVERTED_TO_TASK"
    db.add(CodeCandidate(id=uuid.uuid4(), proposal_id=second.id, correlation_id=uuid.uuid4(), requested_by_agent_id=report.agents["architect"], title="attempt 2", spec={"files_allowed": [MAIN], "acceptance_tests": [], "kind": "code"}, status="rejected"))
    db.commit()
    i = _intent_of(db, _run_as(db, SessionLocal, code_settings, "scout", [again]))
    assert _ev(i.execution_status) == "failed" and "change the approach" in i.error
    assert db.query(ImprovementProposal).filter(ImprovementProposal.title == first.title).count() == 2
