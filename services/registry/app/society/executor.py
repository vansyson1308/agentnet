"""Typed intent execution through EXISTING domain primitives.

Each handler receives an already policy-ALLOWED, schema-valid intent and
performs exactly one bounded action using the same code paths the REST
API uses (AgentChat, Goal, MemoryItem, ImprovementProposal, Offer,
``task_service`` for anything with escrow, the engineering workspace for
code). Handlers:

* never touch ``wallets`` balances/caps, ``agent_capability_grants``,
  ``users`` or secrets — there is no handler for those intent types;
* emit follow-up ``society_events`` with ``causation`` = the triggering
  event and an idempotency key derived from the intent, so a crash between
  the side effect and the intent-status update cannot duplicate an event
  on retry;
* raise ``ExecutionError`` for domain refusals (self-review, wrong state,
  unknown agent) which the worker records on the intent row.

The worker wraps each handler in one DB transaction (see worker.py). A
handful of ``task_service`` calls commit internally; they carry their own
UNIQUE idempotency key so re-execution is still safe.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from .. import task_service
from ..models import (
    Agent,
    AgentCapabilityGrant,
    AgentChat,
    AgentIntent,
    AgentMessageType,
    AgentRun,
    CodeCandidate,
    CodeCandidateStatus,
    CurrencyType,
    Goal,
    GoalOwnerType,
    GoalPriority,
    GoalStatus,
    ImprovementProposal,
    MemoryItem,
    MemoryScope,
    NegotiationRound,
    Offer,
    OfferStatus,
    ProposalScope,
    ProposalSource,
    ProposalStatus,
    SocietyEvent,
)
from .config import SocietySettings
from .engineering import workspace as ws_mod
from .engineering.qa import RISKY_PATH_RE, evaluate_candidate, static_security_scan
from .events import EventType, emit_event, utcnow
from .ids import candidate_id_for
from .intents import IntentType, ValidatedIntent

logger = logging.getLogger(__name__)

# Goals owned by "the society" rather than one agent share this owner id
# (goals.owner_id is NOT NULL and the API requires an owner for SOCIETY too).
SOCIETY_OWNER_ID = uuid.uuid5(uuid.NAMESPACE_URL, "agentnet://society")

_GOAL_TRANSITIONS = {
    GoalStatus.ACTIVE: {GoalStatus.PAUSED, GoalStatus.COMPLETED, GoalStatus.FAILED, GoalStatus.CANCELLED},
    GoalStatus.PAUSED: {GoalStatus.ACTIVE, GoalStatus.CANCELLED},
}
_OPEN_PROPOSAL_STATUSES = (
    ProposalStatus.PROPOSED,
    ProposalStatus.UNDER_REVIEW,
    ProposalStatus.APPROVED,
    ProposalStatus.CONVERTED_TO_TASK,
)
MAX_QA_ATTEMPTS = 2


class ExecutionError(Exception):
    """Domain refusal: recorded on the intent, not retried."""


@dataclass
class ExecContext:
    db: Session
    settings: SocietySettings
    agent: Agent
    grant: AgentCapabilityGrant
    run: AgentRun
    event: SocietyEvent
    intent_row: AgentIntent
    validated: ValidatedIntent
    heartbeat: Callable[[], None] = lambda: None
    now: datetime = field(default_factory=utcnow)


@dataclass
class ExecOutcome:
    result: Dict[str, Any] = field(default_factory=dict)
    events: List[str] = field(default_factory=list)


def _ev(v: Any) -> Any:
    return v.value if hasattr(v, "value") else v


def _emit(ctx: ExecContext, event_type: str, payload: Dict[str, Any], *, subject_type: Optional[str] = None, subject_id: Optional[uuid.UUID] = None, key_suffix: str = "") -> SocietyEvent:
    key = f"intent:{ctx.intent_row.idempotency_key}:{event_type}{(':' + key_suffix) if key_suffix else ''}"
    return emit_event(
        ctx.db,
        event_type=event_type,
        payload=payload,
        actor_type="agent",
        actor_id=ctx.agent.id,
        subject_type=subject_type,
        subject_id=subject_id,
        causation=ctx.event,
        idempotency_key=key[:160],
        source_run_id=ctx.run.id,
        trace_id=ctx.run.trace_id,
    )


def resolve_agent(db: Session, ref: str) -> Agent:
    agent = None
    try:
        agent = db.query(Agent).filter(Agent.id == uuid.UUID(str(ref))).first()
    except (ValueError, AttributeError):
        agent = None
    if agent is None:
        agent = db.query(Agent).filter(Agent.name == ref).first()
    if agent is None:
        raise ExecutionError(f"unknown agent {ref!r}")
    return agent


# ── communication / cognition ─────────────────────────────────────────


def _send_message(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    to_agent = resolve_agent(ctx.db, p.to_agent) if p.to_agent else None
    content_hash = hashlib.sha256(f"{ctx.agent.id}|{to_agent.id if to_agent else ''}|{p.title}|{p.content}".encode("utf-8")).hexdigest()
    window = ctx.settings.repeat_message_window_seconds
    if window > 0:
        dup = (
            ctx.db.query(AgentChat)
            .filter(
                AgentChat.from_agent_id == ctx.agent.id,
                AgentChat.title == p.title,
                AgentChat.content == p.content,
                AgentChat.created_at >= ctx.now - timedelta(seconds=window),
            )
            .first()
        )
        if dup is not None:
            return ExecOutcome(result={"suppressed": "duplicate_message", "existing_message_id": str(dup.id)})
    msg = AgentChat(
        id=uuid.uuid4(),
        from_agent_id=ctx.agent.id,
        to_agent_id=to_agent.id if to_agent else None,
        message_type=AgentMessageType(p.message_type),
        title=p.title,
        content=p.content,
        msg_metadata={"content_hash": content_hash, "run_id": str(ctx.run.id), "correlation_id": str(ctx.run.correlation_id)},
        thread_id=p.thread_id or uuid.uuid4(),
        is_read=False,
    )
    ctx.db.add(msg)
    ctx.db.flush()
    ev = _emit(
        ctx,
        EventType.AGENT_MESSAGE_RECEIVED,
        {
            "message_id": str(msg.id),
            "from_agent": ctx.agent.name,
            "from_agent_id": str(ctx.agent.id),
            "to_agent": to_agent.name if to_agent else None,
            "title": p.title,
            "message_type": p.message_type,
            "thread_id": str(msg.thread_id),
            "content_preview": p.content[:200],
        },
        subject_type="agent" if to_agent else None,
        subject_id=to_agent.id if to_agent else None,
    )
    return ExecOutcome(result={"message_id": str(msg.id), "thread_id": str(msg.thread_id), "to": to_agent.name if to_agent else None}, events=[str(ev.id)])


def _write_memory(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    item = MemoryItem(
        id=uuid.uuid4(),
        agent_id=ctx.agent.id if p.scope == "agent" else None,
        scope=MemoryScope.AGENT if p.scope == "agent" else MemoryScope.SOCIETY,
        title=p.title,
        content=p.content,
        tags=list(p.tags) + [f"run:{ctx.run.id}"],
        source_task_id=p.source_task_id,
        importance=p.importance,
    )
    ctx.db.add(item)
    ctx.db.flush()
    ev = emit_event(
        ctx.db,
        event_type=EventType.MEMORY_WRITTEN,
        payload={"memory_id": str(item.id), "scope": p.scope, "title": p.title},
        actor_type="agent",
        actor_id=ctx.agent.id,
        causation=ctx.event,
        idempotency_key=f"intent:{ctx.intent_row.idempotency_key}:memory",
        source_run_id=ctx.run.id,
        notify=False,
    )
    return ExecOutcome(result={"memory_id": str(item.id), "scope": p.scope}, events=[str(ev.id)])


def _create_goal(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    owner_type = GoalOwnerType.AGENT if p.owner == "agent" else GoalOwnerType.SOCIETY
    owner_id = ctx.agent.id if p.owner == "agent" else SOCIETY_OWNER_ID
    existing = (
        ctx.db.query(Goal)
        .filter(Goal.owner_type == owner_type, Goal.owner_id == owner_id, Goal.title == p.title, Goal.status.in_([GoalStatus.ACTIVE, GoalStatus.PAUSED]))
        .first()
    )
    if existing is not None:
        return ExecOutcome(result={"goal_id": str(existing.id), "duplicate": True})
    if p.parent_goal_id is not None and ctx.db.query(Goal).filter(Goal.id == p.parent_goal_id).first() is None:
        raise ExecutionError("parent goal not found")
    goal = Goal(
        id=uuid.uuid4(),
        title=p.title,
        description=p.description,
        owner_type=owner_type,
        owner_id=owner_id,
        priority=GoalPriority(p.priority),
        status=GoalStatus.ACTIVE,
        success_criteria=list(p.success_criteria),
        parent_goal_id=p.parent_goal_id,
    )
    ctx.db.add(goal)
    ctx.db.flush()
    ev = emit_event(
        ctx.db,
        event_type=EventType.GOAL_CREATED,
        payload={"goal_id": str(goal.id), "title": p.title, "owner": p.owner, "priority": p.priority},
        actor_type="agent",
        actor_id=ctx.agent.id,
        causation=ctx.event,
        idempotency_key=f"intent:{ctx.intent_row.idempotency_key}:goal",
        source_run_id=ctx.run.id,
        notify=False,
    )
    return ExecOutcome(result={"goal_id": str(goal.id)}, events=[str(ev.id)])


def _update_goal(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    goal = ctx.db.query(Goal).filter(Goal.id == p.goal_id).with_for_update().first()
    if goal is None:
        raise ExecutionError("goal not found")
    owned = (goal.owner_type == GoalOwnerType.AGENT and goal.owner_id == ctx.agent.id) or goal.owner_type == GoalOwnerType.SOCIETY
    if not owned:
        raise ExecutionError("agent does not own this goal")
    changes: Dict[str, Any] = {}
    if p.status is not None:
        target = GoalStatus(p.status)
        current = goal.status if isinstance(goal.status, GoalStatus) else GoalStatus(str(goal.status))
        if target != current:
            if target not in _GOAL_TRANSITIONS.get(current, set()):
                raise ExecutionError(f"goal cannot transition {current.value} -> {target.value}")
            goal.status = target
            if target == GoalStatus.COMPLETED:
                goal.completed_at = ctx.now
            changes["status"] = target.value
    if p.priority is not None:
        goal.priority = GoalPriority(p.priority)
        changes["priority"] = p.priority
    if p.note:
        changes["note"] = p.note[:500]
    ev = emit_event(
        ctx.db,
        event_type=EventType.GOAL_UPDATED,
        payload={"goal_id": str(goal.id), "changes": changes},
        actor_type="agent",
        actor_id=ctx.agent.id,
        causation=ctx.event,
        idempotency_key=f"intent:{ctx.intent_row.idempotency_key}:goal-update",
        source_run_id=ctx.run.id,
        notify=False,
    )
    return ExecOutcome(result={"goal_id": str(goal.id), "changes": changes}, events=[str(ev.id)])


def _create_improvement(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    existing = (
        ctx.db.query(ImprovementProposal)
        .filter(ImprovementProposal.title == p.title, ImprovementProposal.status.in_(_OPEN_PROPOSAL_STATUSES))
        .first()
    )
    if existing is not None:
        return ExecOutcome(result={"proposal_id": str(existing.id), "duplicate": True, "status": _ev(existing.status)})
    source = ProposalSource.QA_FAILURE if ctx.grant.role == "qa" else ProposalSource.AUDIT
    proposal = ImprovementProposal(
        id=uuid.uuid4(),
        proposed_by_agent_id=ctx.agent.id,
        source=source,
        title=p.title,
        problem=p.problem,
        root_cause=p.root_cause,
        proposed_change=p.proposed_change,
        expected_benefit=p.expected_benefit,
        risk=p.risk,
        status=ProposalStatus.PROPOSED,
        target_scope=ProposalScope(p.target_scope),
        importance=p.importance,
        source_task_id=p.source_task_id,
    )
    ctx.db.add(proposal)
    ctx.db.flush()
    ev = _emit(
        ctx,
        EventType.PROPOSAL_CREATED,
        {
            "proposal_id": str(proposal.id),
            "title": p.title,
            "importance": p.importance,
            "problem": p.problem[:500],
            "proposed_change": p.proposed_change[:500],
            "proposed_by": ctx.agent.name,
        },
        subject_type="proposal",
        subject_id=proposal.id,
    )
    return ExecOutcome(result={"proposal_id": str(proposal.id)}, events=[str(ev.id)])


def _review_improvement(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    proposal = ctx.db.query(ImprovementProposal).filter(ImprovementProposal.id == p.proposal_id).with_for_update().first()
    if proposal is None:
        raise ExecutionError("proposal not found")
    if proposal.proposed_by_agent_id == ctx.agent.id:
        raise ExecutionError("an agent cannot review its own proposal")
    status = proposal.status if isinstance(proposal.status, ProposalStatus) else ProposalStatus(str(proposal.status))
    if status not in (ProposalStatus.PROPOSED, ProposalStatus.UNDER_REVIEW):
        return ExecOutcome(result={"proposal_id": str(proposal.id), "status": status.value, "unchanged": True})
    proposal.status = ProposalStatus.APPROVED if p.decision == "approve" else ProposalStatus.REJECTED
    payload = {
        "proposal_id": str(proposal.id),
        "title": proposal.title,
        "importance": proposal.importance,
        "problem": (proposal.problem or "")[:500],
        "proposed_change": (proposal.proposed_change or "")[:500],
        "reason": p.reason[:500],
        "reviewer": ctx.agent.name,
    }
    ev = _emit(
        ctx,
        EventType.PROPOSAL_APPROVED if p.decision == "approve" else EventType.PROPOSAL_REJECTED,
        payload,
        subject_type="proposal",
        subject_id=proposal.id,
    )
    return ExecOutcome(result={"proposal_id": str(proposal.id), "status": _ev(proposal.status)}, events=[str(ev.id)])


def _sleep(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    ctx.run.sleep_until = ctx.now + timedelta(seconds=p.seconds)
    return ExecOutcome(result={"sleep_until": ctx.run.sleep_until.isoformat()})


# ── economy ───────────────────────────────────────────────────────────


def _create_offer(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    to_agent = resolve_agent(ctx.db, p.to_agent)
    if to_agent.id == ctx.agent.id:
        raise ExecutionError("cannot make an offer to yourself")
    offer = Offer(
        id=uuid.uuid4(),
        from_agent_id=ctx.agent.id,
        to_agent_id=to_agent.id,
        title=p.title,
        description=p.description,
        price=p.price,
        currency=CurrencyType.CREDITS,
        expires_at=ctx.now + timedelta(seconds=p.expires_in_seconds),
        status=OfferStatus.PENDING,
    )
    ctx.db.add(offer)
    ctx.db.flush()
    ev = _emit(ctx, EventType.OFFER_CREATED, {"offer_id": str(offer.id), "from_agent": ctx.agent.name, "to_agent": to_agent.name, "price": p.price, "title": p.title}, subject_type="agent", subject_id=to_agent.id)
    return ExecOutcome(result={"offer_id": str(offer.id)}, events=[str(ev.id)])


def _counter_offer(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    offer = ctx.db.query(Offer).filter(Offer.id == p.offer_id).with_for_update().first()
    if offer is None:
        raise ExecutionError("offer not found")
    if ctx.agent.id not in (offer.from_agent_id, offer.to_agent_id):
        raise ExecutionError("agent is not a party to this offer")
    if _ev(offer.status) != OfferStatus.PENDING.value:
        raise ExecutionError(f"offer is {_ev(offer.status)}, not pending")
    rounds = ctx.db.query(NegotiationRound).filter(NegotiationRound.offer_id == offer.id).count()
    if rounds >= 5:
        raise ExecutionError("negotiation round limit (5) reached")
    rnd = NegotiationRound(id=uuid.uuid4(), offer_id=offer.id, round_number=rounds + 1, proposed_by_agent_id=ctx.agent.id, proposed_price=p.price, proposed_terms=p.terms, status=OfferStatus.PENDING)
    ctx.db.add(rnd)
    ctx.db.flush()
    other = offer.to_agent_id if ctx.agent.id == offer.from_agent_id else offer.from_agent_id
    ev = _emit(ctx, "offer.countered", {"offer_id": str(offer.id), "round": rounds + 1, "price": p.price, "by": ctx.agent.name}, subject_type="agent", subject_id=other)
    return ExecOutcome(result={"offer_id": str(offer.id), "round": rounds + 1}, events=[str(ev.id)])


def _accept_offer(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    offer = ctx.db.query(Offer).filter(Offer.id == p.offer_id).with_for_update().first()
    if offer is None:
        raise ExecutionError("offer not found")
    if offer.to_agent_id != ctx.agent.id:
        raise ExecutionError("only the recipient can accept an offer")
    if _ev(offer.status) == OfferStatus.ACCEPTED.value:
        return ExecOutcome(result={"offer_id": str(offer.id), "unchanged": True})
    if _ev(offer.status) != OfferStatus.PENDING.value:
        raise ExecutionError(f"offer is {_ev(offer.status)}")
    if offer.expires_at and offer.expires_at < ctx.now:
        offer.status = OfferStatus.EXPIRED
        raise ExecutionError("offer has expired")
    offer.status = OfferStatus.ACCEPTED
    ev = _emit(ctx, EventType.OFFER_ACCEPTED, {"offer_id": str(offer.id), "by": ctx.agent.name, "price": offer.price}, subject_type="agent", subject_id=offer.from_agent_id)
    return ExecOutcome(result={"offer_id": str(offer.id), "status": "accepted"}, events=[str(ev.id)])


def _create_task(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    callee = resolve_agent(ctx.db, p.callee_agent)
    if callee.id == ctx.agent.id:
        raise ExecutionError("cannot create a task for yourself")
    try:
        task, tx = task_service.create_task_with_escrow(
            db=ctx.db,
            caller_agent=ctx.agent,
            callee_agent_id=callee.id,
            capability_name=p.capability,
            input_data=dict(p.input),
            max_budget=p.max_budget,
            currency="credits",
            timeout_seconds=p.timeout_seconds,
            parent_span_id=ctx.run.span_id,
            idempotency_key=ctx.intent_row.idempotency_key[:64],
        )
    except task_service.EscrowError as exc:
        raise ExecutionError(f"escrow refused: {exc}") from exc
    if p.proposal_id is not None:
        proposal = ctx.db.query(ImprovementProposal).filter(ImprovementProposal.id == p.proposal_id).first()
        if proposal is not None and _ev(proposal.status) == ProposalStatus.APPROVED.value:
            proposal.status = ProposalStatus.CONVERTED_TO_TASK
            proposal.converted_task_id = task.id
    ev = _emit(
        ctx,
        EventType.TASK_CREATED,
        {"task_id": str(task.id), "capability": p.capability, "escrow_amount": task.escrow_amount, "caller": ctx.agent.name, "callee": callee.name, "goal_id": str(p.goal_id) if p.goal_id else None},
        subject_type="agent",
        subject_id=callee.id,
    )
    return ExecOutcome(result={"task_id": str(task.id), "transaction_id": str(tx.id), "escrow_amount": task.escrow_amount}, events=[str(ev.id)])


def _start_task(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    try:
        task = task_service.start_task(db=ctx.db, task_id=p.task_id, callee_agent=ctx.agent)
    except task_service.EscrowError as exc:
        raise ExecutionError(str(exc)) from exc
    return ExecOutcome(result={"task_id": str(task.id), "status": _ev(task.status)})


def _complete_task(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    try:
        task = task_service.confirm_task_completion(db=ctx.db, callee_agent=ctx.agent, task_id=p.task_id, output=dict(p.output))
    except task_service.EscrowError as exc:
        raise ExecutionError(str(exc)) from exc
    ev = _emit(ctx, EventType.TASK_COMPLETED, {"task_id": str(task.id), "capability": task.capability, "callee": ctx.agent.name}, subject_type="agent", subject_id=task.caller_agent_id)
    return ExecOutcome(result={"task_id": str(task.id), "status": _ev(task.status)}, events=[str(ev.id)])


def _fail_task(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    try:
        task = task_service.fail_task_with_refund(db=ctx.db, task_id=p.task_id, error_message=p.error, callee_agent_id=ctx.agent.id)
    except task_service.EscrowError as exc:
        raise ExecutionError(str(exc)) from exc
    ev = _emit(ctx, EventType.TASK_FAILED, {"task_id": str(task.id), "capability": task.capability, "error": p.error[:500], "callee": ctx.agent.name}, subject_type="task", subject_id=task.id)
    return ExecOutcome(result={"task_id": str(task.id), "status": _ev(task.status)}, events=[str(ev.id)])


# ── engineering loop ──────────────────────────────────────────────────


def _get_candidate(ctx: ExecContext, candidate_id: uuid.UUID) -> CodeCandidate:
    cand = ctx.db.query(CodeCandidate).filter(CodeCandidate.id == candidate_id).with_for_update().first()
    if cand is None:
        raise ExecutionError("code candidate not found")
    return cand


def _request_code_change(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    spec = p.spec.model_dump()
    for f in spec["files_allowed"]:
        if ws_mod.is_protected(f):
            raise ExecutionError(f"spec allows a protected path: {f}")
    if p.proposal_id is not None:
        existing = (
            ctx.db.query(CodeCandidate)
            .filter(CodeCandidate.proposal_id == p.proposal_id, CodeCandidate.status.notin_([CodeCandidateStatus.REJECTED, CodeCandidateStatus.FAILED, CodeCandidateStatus.ABANDONED]))
            .first()
        )
        if existing is not None:
            return ExecOutcome(result={"candidate_id": str(existing.id), "duplicate": True, "status": _ev(existing.status)})
    requires_sec = bool(p.requires_security_review) or any(RISKY_PATH_RE.search(f) for f in spec["files_allowed"]) or spec.get("kind") == "code"
    cand_id = candidate_id_for(ctx.run.correlation_id, p.proposal_id, p.title)
    existing_by_id = ctx.db.query(CodeCandidate).filter(CodeCandidate.id == cand_id).first()
    if existing_by_id is not None:
        return ExecOutcome(result={"candidate_id": str(existing_by_id.id), "duplicate": True, "status": _ev(existing_by_id.status)})
    cand = CodeCandidate(
        id=cand_id,
        proposal_id=p.proposal_id,
        task_id=p.task_id,
        goal_id=p.goal_id,
        correlation_id=ctx.run.correlation_id,
        requested_by_agent_id=ctx.agent.id,
        title=p.title,
        spec=spec,
        status=CodeCandidateStatus.REQUESTED,
        requires_security_review=requires_sec,
    )
    ctx.db.add(cand)
    ctx.db.flush()
    if p.proposal_id is not None:
        proposal = ctx.db.query(ImprovementProposal).filter(ImprovementProposal.id == p.proposal_id).first()
        if proposal is not None and _ev(proposal.status) == ProposalStatus.APPROVED.value:
            proposal.status = ProposalStatus.CONVERTED_TO_TASK
    ev = _emit(
        ctx,
        EventType.CODE_CHANGE_REQUESTED,
        {"candidate_id": str(cand.id), "title": p.title, "files_allowed": spec["files_allowed"], "acceptance_tests": spec["acceptance_tests"], "proposal_id": str(p.proposal_id) if p.proposal_id else None, "requires_security_review": requires_sec},
        subject_type="code_candidate",
        subject_id=cand.id,
    )
    return ExecOutcome(result={"candidate_id": str(cand.id), "requires_security_review": requires_sec}, events=[str(ev.id)])


def _submit_code_candidate(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    cand = _get_candidate(ctx, p.candidate_id)
    status = _ev(cand.status)
    if status not in (CodeCandidateStatus.REQUESTED.value, CodeCandidateStatus.QA_FAILED.value, CodeCandidateStatus.BUILDING.value):
        raise ExecutionError(f"candidate is {status}; cannot submit")
    if cand.builder_agent_id is not None and cand.builder_agent_id != ctx.agent.id:
        raise ExecutionError("candidate is owned by another builder")
    if cand.requested_by_agent_id == ctx.agent.id:
        raise ExecutionError("the requesting agent cannot also build the candidate")
    spec = cand.spec or {}
    cand.status = CodeCandidateStatus.BUILDING
    cand.builder_agent_id = ctx.agent.id
    cand.builder_run_id = ctx.run.id
    ctx.db.flush()
    try:
        ws = ws_mod.ensure_workspace(ctx.settings, cand.id)
        ctx.heartbeat()
        written = ws_mod.apply_edits(ws, p.edits, allowed=spec.get("files_allowed") or [])
        head = ws_mod.commit_all(ws, f"society: {cand.title} (candidate {cand.id})\n\n{p.summary}")
        changed = ws_mod.changed_files(ws)
        stat = ws_mod.diff_stat(ws)
    except ws_mod.WorkspaceError as exc:
        # The lease heartbeat may already have committed status=BUILDING;
        # persist the reset explicitly so the worker's rollback cannot leave
        # the candidate stuck in BUILDING.
        cand.status = CodeCandidateStatus.REQUESTED if status != CodeCandidateStatus.QA_FAILED.value else CodeCandidateStatus.QA_FAILED
        cand.error = str(exc)[:2000]
        ctx.db.commit()
        raise ExecutionError(f"workspace refused: {exc}") from exc
    cand.status = CodeCandidateStatus.BUILT
    cand.branch_name = ws.branch
    cand.workspace_path = str(ws.path)
    cand.base_sha = ws.base_sha
    cand.head_sha = head
    cand.diff_stat = stat
    cand.changed_files = changed
    cand.patch_summary = p.summary
    cand.error = None
    ev = _emit(
        ctx,
        EventType.CODE_CANDIDATE_BUILT,
        {"candidate_id": str(cand.id), "title": cand.title, "head_sha": head, "branch_name": ws.branch, "changed_files": changed, "requires_security_review": bool(cand.requires_security_review)},
        subject_type="code_candidate",
        subject_id=cand.id,
        key_suffix=head[:12],
    )
    return ExecOutcome(result={"candidate_id": str(cand.id), "branch": ws.branch, "head_sha": head, "written": written, "changed_files": changed}, events=[str(ev.id)])


def _request_qa(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    cand = _get_candidate(ctx, p.candidate_id)
    if _ev(cand.status) != CodeCandidateStatus.BUILT.value:
        raise ExecutionError(f"candidate is {_ev(cand.status)}; QA can only be requested for BUILT candidates")
    ev = _emit(ctx, EventType.CODE_CANDIDATE_BUILT, {"candidate_id": str(cand.id), "title": cand.title, "head_sha": cand.head_sha, "branch_name": cand.branch_name, "changed_files": list(cand.changed_files or []), "requeued": True}, subject_type="code_candidate", subject_id=cand.id, key_suffix=f"requeue:{cand.head_sha or ''}"[:40])
    return ExecOutcome(result={"candidate_id": str(cand.id)}, events=[str(ev.id)])


def _finish_candidate(ctx: ExecContext, cand: CodeCandidate, *, ready: bool, summary: str) -> List[str]:
    events = []
    cand.status = CodeCandidateStatus.READY if ready else CodeCandidateStatus.REJECTED
    payload = {"candidate_id": str(cand.id), "title": cand.title, "branch_name": cand.branch_name, "head_sha": cand.head_sha, "qa_summary": summary[:500], "proposal_id": str(cand.proposal_id) if cand.proposal_id else None}
    ev = _emit(ctx, EventType.CODE_CANDIDATE_READY if ready else EventType.CODE_CANDIDATE_REJECTED, payload, subject_type="code_candidate", subject_id=cand.id)
    events.append(str(ev.id))
    return events


def _evaluate_code_candidate(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    cand = _get_candidate(ctx, p.candidate_id)
    status = _ev(cand.status)
    if status not in (CodeCandidateStatus.BUILT.value, CodeCandidateStatus.QA_RUNNING.value):
        raise ExecutionError(f"candidate is {status}; only BUILT candidates can be evaluated")
    if cand.builder_agent_id == ctx.agent.id:
        raise ExecutionError("QA independence: the builder cannot evaluate its own candidate")
    if cand.requested_by_agent_id == ctx.agent.id:
        raise ExecutionError("QA independence: the requester cannot evaluate the candidate")
    prev_attempts = int((cand.qa_report or {}).get("attempts") or 0)
    cand.status = CodeCandidateStatus.QA_RUNNING
    cand.qa_agent_id = ctx.agent.id
    cand.qa_run_id = ctx.run.id
    ctx.db.flush()
    ctx.heartbeat()
    try:
        ws = ws_mod.ensure_workspace(ctx.settings, cand.id)
        report = evaluate_candidate(ctx.settings, ws, cand.spec or {}, list(cand.changed_files or []), attempts=prev_attempts + 1)
    except ws_mod.WorkspaceError as exc:
        cand.status = CodeCandidateStatus.BUILT  # heartbeat committed QA_RUNNING; persist the reset
        cand.error = str(exc)[:2000]
        ctx.db.commit()
        raise ExecutionError(f"workspace unavailable for QA: {exc}") from exc
    ctx.heartbeat()
    report_dict = report.to_dict()
    findings = list(getattr(report, "static_findings", []) or [])
    report_dict["static_findings"] = findings
    report_dict["evaluated_by"] = ctx.agent.name
    report_dict["run_id"] = str(ctx.run.id)
    report_dict["head_sha"] = cand.head_sha
    cand.qa_report = report_dict
    sec = dict(cand.security_report or {})
    sec["static_findings"] = findings
    cand.security_report = sec
    events: List[str] = []
    if report.passed:
        cand.status = CodeCandidateStatus.QA_PASSED
        ev = _emit(ctx, EventType.CODE_CANDIDATE_QA_PASSED, {"candidate_id": str(cand.id), "title": cand.title, "attempts": report.attempts, "qa_summary": report.summary}, subject_type="code_candidate", subject_id=cand.id)
        events.append(str(ev.id))
        needs_security = bool(cand.requires_security_review) or bool(findings)
        if needs_security:
            cand.status = CodeCandidateStatus.SECURITY_REVIEW
            ev2 = _emit(ctx, EventType.CODE_CANDIDATE_SECURITY_REVIEW, {"candidate_id": str(cand.id), "title": cand.title, "changed_files": list(cand.changed_files or []), "static_findings": findings, "qa_summary": report.summary}, subject_type="code_candidate", subject_id=cand.id)
            events.append(str(ev2.id))
        else:
            events += _finish_candidate(ctx, cand, ready=True, summary=report.summary)
    else:
        if report.attempts >= MAX_QA_ATTEMPTS:
            events += _finish_candidate(ctx, cand, ready=False, summary=report.summary)
        else:
            cand.status = CodeCandidateStatus.QA_FAILED
        ev = _emit(ctx, EventType.CODE_CANDIDATE_QA_FAILED, {"candidate_id": str(cand.id), "title": cand.title, "attempts": report.attempts, "qa_summary": report.summary, "failures": report.failures[:5], "final": report.attempts >= MAX_QA_ATTEMPTS}, subject_type="code_candidate", subject_id=cand.id, key_suffix=f"attempt{report.attempts}")
        events.append(str(ev.id))
    return ExecOutcome(result={"candidate_id": str(cand.id), "verdict": report.verdict, "attempts": report.attempts, "summary": report.summary, "status": _ev(cand.status)}, events=events)


def _security_review_candidate(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    cand = _get_candidate(ctx, p.candidate_id)
    if _ev(cand.status) != CodeCandidateStatus.SECURITY_REVIEW.value:
        raise ExecutionError(f"candidate is {_ev(cand.status)}; not awaiting security review")
    if ctx.agent.id in (cand.builder_agent_id, cand.qa_agent_id, cand.requested_by_agent_id):
        raise ExecutionError("security independence: builder/QA/requester cannot perform the security review")
    try:
        ws = ws_mod.ensure_workspace(ctx.settings, cand.id)
        static = static_security_scan(ws, list(cand.changed_files or []))
    except ws_mod.WorkspaceError as exc:
        raise ExecutionError(f"workspace unavailable for security review: {exc}") from exc
    final_pass = p.verdict == "pass" and not static
    cand.security_agent_id = ctx.agent.id
    cand.security_run_id = ctx.run.id
    cand.security_report = {
        "verdict": "pass" if final_pass else "fail",
        "model_verdict": p.verdict,
        "findings": list(p.findings),
        "static_findings": static,
        "reviewed_by": ctx.agent.name,
        "run_id": str(ctx.run.id),
        "head_sha": cand.head_sha,
    }
    summary = f"security {'PASS' if final_pass else 'FAIL'}: {len(static)} static finding(s), {len(p.findings)} reviewer finding(s)"
    events = _finish_candidate(ctx, cand, ready=final_pass, summary=summary)
    return ExecOutcome(result={"candidate_id": str(cand.id), "verdict": "pass" if final_pass else "fail", "static_findings": static}, events=events)


def _request_staging_deploy(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    cand = _get_candidate(ctx, p.candidate_id)
    if _ev(cand.status) != CodeCandidateStatus.READY.value:
        raise ExecutionError("only READY candidates can be proposed for staging")
    ev = _emit(ctx, EventType.STAGING_DEPLOY_REQUESTED, {"candidate_id": str(cand.id), "branch_name": cand.branch_name, "head_sha": cand.head_sha, "note": "recorded only — no deploy is executed by the runtime in v1"}, subject_type="code_candidate", subject_id=cand.id)
    return ExecOutcome(result={"candidate_id": str(cand.id), "note": "staging deploy request recorded; deployment is a human/CI action in v1"}, events=[str(ev.id)])


HANDLERS: Dict[IntentType, Callable[[ExecContext], ExecOutcome]] = {
    IntentType.SEND_MESSAGE: _send_message,
    IntentType.WRITE_MEMORY: _write_memory,
    IntentType.CREATE_GOAL: _create_goal,
    IntentType.UPDATE_GOAL: _update_goal,
    IntentType.CREATE_IMPROVEMENT: _create_improvement,
    IntentType.REVIEW_IMPROVEMENT: _review_improvement,
    IntentType.SLEEP: _sleep,
    IntentType.CREATE_OFFER: _create_offer,
    IntentType.COUNTER_OFFER: _counter_offer,
    IntentType.ACCEPT_OFFER: _accept_offer,
    IntentType.CREATE_TASK: _create_task,
    IntentType.START_TASK: _start_task,
    IntentType.COMPLETE_TASK: _complete_task,
    IntentType.FAIL_TASK: _fail_task,
    IntentType.REQUEST_CODE_CHANGE: _request_code_change,
    IntentType.SUBMIT_CODE_CANDIDATE: _submit_code_candidate,
    IntentType.REQUEST_QA: _request_qa,
    IntentType.EVALUATE_CODE_CANDIDATE: _evaluate_code_candidate,
    IntentType.SECURITY_REVIEW_CANDIDATE: _security_review_candidate,
    IntentType.REQUEST_STAGING_DEPLOY: _request_staging_deploy,
}


def execute(ctx: ExecContext) -> ExecOutcome:
    itype = ctx.validated.intent_type
    handler = HANDLERS.get(itype) if itype is not None else None
    if handler is None:
        raise ExecutionError(f"no executor for {ctx.validated.type_name} (fail closed)")
    return handler(ctx)
