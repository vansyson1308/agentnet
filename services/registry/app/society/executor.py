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
import pathlib
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
    ChangeExperiment,
    CodeCandidate,
    CodeCandidateStatus,
    CodePromotion,
    CurrencyType,
    Goal,
    GoalOwnerType,
    GoalPriority,
    GoalStatus,
    ImprovementProposal,
    IntentExecutionStatus,
    MemoryItem,
    MemoryScope,
    NegotiationRound,
    Offer,
    OfferStatus,
    ProposalScope,
    ProposalSource,
    ProposalStatus,
    RiskTier,
    SocietyEvent,
    TaskSession,
)
from . import repo_intel
from .config import SocietySettings
from .engineering import workspace as ws_mod
from .engineering.qa import RISKY_PATH_RE, evaluate_candidate, static_security_scan
from .events import REHEARSAL_MEMORY_TTL_SECONDS, EventType, emit_event, is_rehearsal_correlation, utcnow
from .ids import candidate_id_for
from .intents import REPO_READ_INTENT_TYPES, IntentType, ValidatedIntent
from .risk import assess as assess_risk

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
    deployment_provider: Any = None   # test injection; production resolves from settings


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


def _require_ref(ctx: ExecContext, model, ref: Optional[uuid.UUID], label: str) -> Optional[uuid.UUID]:
    """Model-supplied identifiers are UNTRUSTED: a fabricated ``source_task_id``
    / ``goal_id`` / ``proposal_id`` must fail the intent with a clear reason,
    never reach a foreign key and crash the executor mid-transaction."""
    if ref is None:
        return None
    if ctx.db.query(model.id).filter(model.id == ref).first() is None:
        raise ExecutionError(f"{label} does not reference an existing {model.__tablename__.rstrip('s').replace('_', ' ')}: model-supplied ids are validated, never trusted")
    return ref


def _write_memory(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    _require_ref(ctx, TaskSession, p.source_task_id, "source_task_id")
    # A canary rehearsal must not train the fleet permanently: memory written
    # under a rehearsal correlation expires with it (see events.py). The row is
    # still written, linked and auditable — it just stops being read back as
    # prior experience once the rehearsal is over.
    expires_at = (
        utcnow() + timedelta(seconds=REHEARSAL_MEMORY_TTL_SECONDS)
        if is_rehearsal_correlation(ctx.db, ctx.run.correlation_id)
        else None
    )
    item = MemoryItem(
        id=uuid.uuid4(),
        expires_at=expires_at,
        agent_id=ctx.agent.id if p.scope == "agent" else None,
        scope=MemoryScope.AGENT if p.scope == "agent" else MemoryScope.SOCIETY,
        title=p.title,
        content=p.content,
        tags=[tg for tg in p.tags if tg.lower() not in ("policy", "trusted", "validated")] + [f"run:{ctx.run.id}"],
        source_task_id=p.source_task_id,
        importance=p.importance,
        # Provenance is set by trusted code, never by the payload: an agent
        # cannot mark its own memory validated/policy.
        source_type="run",
        source_id=ctx.run.id,
        correlation_id=ctx.run.correlation_id,
        author_agent_id=ctx.agent.id,
        confidence=min(int(p.importance), 70),
        validation_state="unvalidated",
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


WORLD_SIGNAL_EVENTS = frozenset(
    {
        EventType.PLATFORM_METRIC_ANOMALY,
        EventType.PLATFORM_HEALTH_DEGRADED,
        EventType.USER_FEEDBACK_RECEIVED,
        EventType.STAGING_CANARY_SIGNAL,
        EventType.TASK_FAILED,
        EventType.TASK_TIMEOUT,
        EventType.QA_FAILED,
        EventType.AGENT_INACTIVE,
        EventType.RUN_DEAD,
    }
)


def _create_improvement(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    _require_ref(ctx, TaskSession, p.source_task_id, "source_task_id")
    if ctx.event.event_type in WORLD_SIGNAL_EVENTS and p.evidence is None:
        # An event existing is not evidence. Signal-driven proposals must say
        # what was observed, against what baseline, over what window, and why
        # it is actionable (anti-busywork; docs/SELF_DEVELOPMENT.md).
        raise ExecutionError("signal-driven proposals must carry evidence (signal, baseline, observed, window, sample, actionable_reason)")
    existing = (
        ctx.db.query(ImprovementProposal)
        .filter(ImprovementProposal.title == p.title, ImprovementProposal.status.in_(_OPEN_PROPOSAL_STATUSES))
        .first()
    )
    if existing is not None:
        return ExecOutcome(result={"proposal_id": str(existing.id), "duplicate": True, "status": _ev(existing.status)})
    if ctx.settings.company_cycle_enabled:
        # Company-mode portfolio cap (ADR-0009 D15): a few hypotheses pursued
        # to a conclusion beat many started. Close or reject one first.
        open_count = (
            ctx.db.query(ImprovementProposal)
            .filter(ImprovementProposal.proposed_by_agent_id.isnot(None), ImprovementProposal.status.in_(_OPEN_PROPOSAL_STATUSES))
            .count()
        )
        if open_count >= ctx.settings.company_max_active_hypotheses:
            raise ExecutionError(
                f"portfolio full: {open_count} active hypotheses (SOCIETY_COMPANY_MAX_ACTIVE_HYPOTHESES="
                f"{ctx.settings.company_max_active_hypotheses}); conclude one before opening another"
            )
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


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _candidates_today(ctx: ExecContext, *, red_only: bool = False) -> int:
    q = ctx.db.query(CodeCandidate).filter(CodeCandidate.created_at >= _day_start(ctx.now))
    if red_only:
        q = q.filter(CodeCandidate.risk_tier.in_([RiskTier.RED.value, RiskTier.NEVER.value]))
    return int(q.count())


def _enforce_docs_contract(ctx: ExecContext, spec: Dict[str, Any], p) -> None:
    """Refuse a docs spec the trusted QA gate could never pass, ONCE correctably.

    The Architect gets exactly one corrective turn: the first refusal for a
    proposal emits ``code_change.spec_rejected`` carrying machine-readable
    errors, which wakes it. A second refusal for the same proposal is still
    refused but emits nothing, so a model that cannot satisfy the contract
    cannot spin on it. The errors say what the boundary is, never what to
    write: the filename, the title and the content stay the Architect's.
    """
    from .engineering import docs_contract

    root = pathlib.Path(ctx.settings.repo_root) if getattr(ctx.settings, "repo_root", "") else None
    errors = docs_contract.validate_docs_spec(spec, repo_root=root)
    if not errors:
        return
    detail = "; ".join(f"{e.field}: {e.code} (expected {e.expected})" for e in errors)
    already = 0
    if p.proposal_id is not None:
        already = (
            ctx.db.query(SocietyEvent.id)
            .filter(
                SocietyEvent.event_type == EventType.CODE_CHANGE_SPEC_REJECTED,
                SocietyEvent.correlation_id == ctx.run.correlation_id,
            )
            .count()
        )
    if already == 0:
        _emit(
            ctx,
            EventType.CODE_CHANGE_SPEC_REJECTED,
            {
                "proposal_id": str(p.proposal_id) if p.proposal_id else None,
                "title": p.title,
                "kind": spec.get("kind"),
                "valid": False,
                "errors": [e.as_dict() for e in errors],
                "corrective_turns_remaining": 0,
            },
            subject_type="improvement_proposal",
            subject_id=p.proposal_id,
        )
    raise ExecutionError(f"docs candidate spec violates the engineering contract -> {detail}")


def _request_code_change(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    spec = p.spec.model_dump()
    for f in spec["files_allowed"]:
        if ws_mod.is_protected(f):
            raise ExecutionError(f"spec allows a never-writable path: {f}")
    if len(spec["files_allowed"]) > ctx.settings.max_files_per_candidate:
        raise ExecutionError(f"spec allows {len(spec['files_allowed'])} files; change budget is {ctx.settings.max_files_per_candidate} per candidate")
    if ctx.settings.company_cycle_enabled:
        open_red = (
            ctx.db.query(CodeCandidate)
            .filter(
                CodeCandidate.risk_tier == RiskTier.RED.value,
                CodeCandidate.status.notin_([CodeCandidateStatus.READY, CodeCandidateStatus.REJECTED, CodeCandidateStatus.FAILED, CodeCandidateStatus.ABANDONED]),
            )
            .count()
        )
        if open_red >= ctx.settings.company_max_high_risk_investigations:
            raise ExecutionError(
                f"portfolio full: {open_red} open high-risk investigation(s) (SOCIETY_COMPANY_MAX_HIGH_RISK_INVESTIGATIONS="
                f"{ctx.settings.company_max_high_risk_investigations}); finish it first"
            )
    # Anti-busywork: every autonomous engineering effort links signal -> proposal
    # -> expected effect -> acceptance criteria. Docs candidates need the
    # proposal link; code candidates additionally need an expected effect.
    if p.proposal_id is None:
        raise ExecutionError("a code change must link to an improvement proposal (proposal_id); unlinked engineering is busywork")
    _require_ref(ctx, ImprovementProposal, p.proposal_id, "proposal_id")
    _require_ref(ctx, TaskSession, p.task_id, "task_id")
    _require_ref(ctx, Goal, p.goal_id, "goal_id")
    if spec.get("kind") == "code" and not (spec.get("expected_effect") or "").strip():
        raise ExecutionError("code candidates must state expected_effect (the metric/behaviour the change should move)")
    if not spec.get("acceptance_tests"):
        raise ExecutionError("a code change must name acceptance tests; QA never fabricates criteria")
    # Design-time contract, LAST: the generic rules (proposal link, acceptance
    # criteria, expected effect) own their own error messages, and this one is
    # specific to the docs convention QA enforces mechanically. Refusing here
    # means the candidate is never created, instead of the Builder discovering
    # three steps later that it cannot produce a conforming diff.
    if spec.get("kind") == "docs":
        _enforce_docs_contract(ctx, spec, p)
    prelim = assess_risk(list(spec["files_allowed"]), "", spec_kind=str(spec.get("kind") or ""))
    if _candidates_today(ctx) >= ctx.settings.max_autonomous_candidates_per_day:
        raise ExecutionError(f"change budget exhausted: {ctx.settings.max_autonomous_candidates_per_day} autonomous candidates today")
    if prelim.tier in (RiskTier.RED, RiskTier.NEVER) and _candidates_today(ctx, red_only=True) >= ctx.settings.max_red_candidates_per_day:
        raise ExecutionError(f"change budget exhausted: {ctx.settings.max_red_candidates_per_day} RED candidates today")
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
        requires_security_review=requires_sec or prelim.tier in (RiskTier.RED, RiskTier.NEVER),
        risk_tier=prelim.tier.value,  # preliminary (paths only); the controller re-classifies from the real diff
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
    diff_hash, diff_lines = ws_mod.diff_identity(ws)
    busywork = None
    if not changed or diff_lines == 0:
        busywork = "no-op change: nothing differs from the base revision"
    elif ws_mod.is_format_only(ws):
        busywork = "format-only churn: the diff changes whitespace only"
    elif diff_lines > ctx.settings.max_diff_lines:
        busywork = f"diff of {diff_lines} lines exceeds the change budget ({ctx.settings.max_diff_lines})"
    elif len(changed) > ctx.settings.max_files_per_candidate:
        busywork = f"{len(changed)} files changed; change budget is {ctx.settings.max_files_per_candidate}"
    else:
        dup = (
            ctx.db.query(CodeCandidate)
            .filter(CodeCandidate.diff_hash == diff_hash, CodeCandidate.id != cand.id, CodeCandidate.status.notin_([CodeCandidateStatus.REJECTED, CodeCandidateStatus.FAILED, CodeCandidateStatus.ABANDONED]))
            .first()
        )
        if dup is not None:
            busywork = f"duplicate candidate: identical diff already open as {dup.id}"
    if busywork:
        cand.status = CodeCandidateStatus.REJECTED
        cand.error = busywork
        cand.diff_hash = diff_hash
        cand.diff_lines = diff_lines
        cand.changed_files = changed
        cand.head_sha = head
        cand.branch_name = ws.branch
        ev = _emit(ctx, EventType.CODE_CANDIDATE_REJECTED, {"candidate_id": str(cand.id), "title": cand.title, "branch_name": ws.branch, "head_sha": head, "qa_summary": f"anti-busywork: {busywork}", "proposal_id": str(cand.proposal_id) if cand.proposal_id else None, "busywork": True}, subject_type="code_candidate", subject_id=cand.id, key_suffix="busywork")
        ctx.db.commit()
        raise ExecutionError(f"candidate rejected (anti-busywork): {busywork}")
    cand.status = CodeCandidateStatus.BUILT
    cand.branch_name = ws.branch
    cand.workspace_path = str(ws.path)
    cand.base_sha = ws.base_sha
    cand.head_sha = head
    cand.diff_stat = stat
    cand.changed_files = changed
    cand.patch_summary = p.summary
    cand.diff_hash = diff_hash
    cand.diff_lines = diff_lines
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
    from . import deployment as dep_mod

    p = ctx.validated.payload
    cand = _get_candidate(ctx, p.candidate_id)
    if _ev(cand.status) != CodeCandidateStatus.READY.value:
        raise ExecutionError("only READY candidates can be proposed for staging")
    promo = ctx.db.query(CodePromotion).filter(CodePromotion.candidate_id == cand.id).order_by(CodePromotion.created_at.desc()).first()
    if promo is None or _ev(promo.status) != "merged":
        raise ExecutionError("staging deployment requires a MERGED promotion; request promotion first")
    provider = dep_mod.get_deployment_provider(ctx.settings, override=getattr(ctx, "deployment_provider", None))
    req = dep_mod.request_deployment(ctx.db, settings=ctx.settings, provider=provider, environment=dep_mod.STAGING, candidate=cand, promotion=promo, correlation_id=ctx.run.correlation_id, target_sha=promo.merged_sha or cand.head_sha, requested_by_agent_id=ctx.agent.id, causation=ctx.event, source_run_id=ctx.run.id)
    ev = _emit(ctx, EventType.STAGING_DEPLOY_REQUESTED, {"candidate_id": str(cand.id), "promotion_id": str(promo.id), "request_id": str(req.id), "status": _ev(req.status), "note": req.note}, subject_type="deployment_request", subject_id=req.id)
    return ExecOutcome(result={"candidate_id": str(cand.id), "request_id": str(req.id), "status": _ev(req.status), "note": req.note}, events=[str(ev.id)])


# ── promotion / evaluation requests (the controller decides) ─────────


def _request_pr_promotion(ctx: ExecContext) -> ExecOutcome:
    from .promotion import request_promotion

    p = ctx.validated.payload
    cand = _get_candidate(ctx, p.candidate_id)
    if _ev(cand.status) != CodeCandidateStatus.READY.value:
        raise ExecutionError(f"candidate is {_ev(cand.status)}; only READY candidates can be promoted")
    if ctx.agent.id == cand.builder_agent_id:
        raise ExecutionError("promotion independence: the Builder cannot request promotion of its own candidate")
    try:
        promo, created = request_promotion(ctx.db, settings=ctx.settings, candidate=cand, agent=ctx.agent, run=ctx.run, causation=ctx.event, source_run_id=ctx.run.id)
    except ValueError as exc:
        raise ExecutionError(str(exc)) from exc
    return ExecOutcome(result={"promotion_id": str(promo.id), "candidate_id": str(cand.id), "status": _ev(promo.status), "duplicate": not created, "note": "recorded; the promotion controller validates, publishes and tracks CI — nothing merges automatically"})


def _request_merge_evaluation(ctx: ExecContext) -> ExecOutcome:
    from .fitness import request_experiment

    p = ctx.validated.payload
    promo = ctx.db.query(CodePromotion).filter(CodePromotion.id == p.promotion_id).with_for_update().first()
    if promo is None:
        raise ExecutionError("promotion not found")
    cand = ctx.db.query(CodeCandidate).filter(CodeCandidate.id == promo.candidate_id).first()
    if cand is None:
        raise ExecutionError("candidate not found")
    if ctx.agent.id in (cand.builder_agent_id, cand.qa_agent_id, cand.security_agent_id):
        raise ExecutionError("evaluation independence: Builder/QA/Security cannot request the fitness evaluation")
    if _ev(promo.status) not in ("ci_passed", "awaiting_approval", "merge_eligible", "merged", "pr_open", "ci_pending", "branch_ready", "blocked_external"):
        raise ExecutionError(f"promotion is {_ev(promo.status)}; nothing to evaluate")
    exp, created = request_experiment(ctx.db, settings=ctx.settings, promotion=promo, candidate=cand, agent=ctx.agent, causation=ctx.event, source_run_id=ctx.run.id)
    return ExecOutcome(result={"experiment_id": str(exp.id), "promotion_id": str(promo.id), "status": _ev(exp.status), "duplicate": not created, "mode": exp.evaluation_mode})


def _request_staging_evaluation(ctx: ExecContext) -> ExecOutcome:
    from . import deployment as dep_mod

    p = ctx.validated.payload
    promo = ctx.db.query(CodePromotion).filter(CodePromotion.id == p.promotion_id).first()
    if promo is None:
        raise ExecutionError("promotion not found")
    if _ev(promo.status) != "merged":
        raise ExecutionError("staging evaluation requires a MERGED promotion")
    cand = ctx.db.query(CodeCandidate).filter(CodeCandidate.id == promo.candidate_id).first()
    provider = dep_mod.get_deployment_provider(ctx.settings, override=getattr(ctx, "deployment_provider", None))
    req = dep_mod.request_deployment(ctx.db, settings=ctx.settings, provider=provider, environment=dep_mod.STAGING, candidate=cand, promotion=promo, correlation_id=ctx.run.correlation_id, target_sha=promo.merged_sha, requested_by_agent_id=ctx.agent.id, causation=ctx.event, source_run_id=ctx.run.id)
    return ExecOutcome(result={"request_id": str(req.id), "status": _ev(req.status), "note": req.note, "environment": req.environment})


def _record_evaluation_recommendation(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    exp = ctx.db.query(ChangeExperiment).filter(ChangeExperiment.id == p.experiment_id).with_for_update().first()
    if exp is None:
        raise ExecutionError("experiment not found")
    if _ev(exp.status) not in ("pass", "fail", "inconclusive"):
        raise ExecutionError(f"experiment is {_ev(exp.status)}; recommendations are recorded only after the deterministic decision")
    cand = ctx.db.query(CodeCandidate).filter(CodeCandidate.id == exp.candidate_id).first()
    if cand is not None and ctx.agent.id in (cand.builder_agent_id, cand.qa_agent_id, cand.security_agent_id):
        raise ExecutionError("evaluation independence: Builder/QA/Security cannot recommend on their own candidate")
    # Opinion only: the decision, gates, metrics and criteria stay untouched.
    exp.recommendation = {"by": ctx.agent.name, "agent_id": str(ctx.agent.id), "run_id": str(ctx.run.id), "recommendation": p.recommendation, "summary": p.summary[:2000], "decision_at_time": exp.decision, "recorded_at": ctx.now.isoformat()}
    return ExecOutcome(result={"experiment_id": str(exp.id), "recommendation": p.recommendation, "decision": exp.decision, "note": "advisory only; hard gates and the persisted decision are unchanged"})


# ── repository intelligence (read-only, bounded, audited) ─────────────


def _read_root(ctx: ExecContext, candidate_id: Optional[uuid.UUID]):
    """Trusted base checkout, or the candidate's isolated worktree."""
    import pathlib

    if candidate_id is None:
        return pathlib.Path(ctx.settings.repo_root), None
    cand = ctx.db.query(CodeCandidate).filter(CodeCandidate.id == candidate_id).first()
    if cand is None:
        raise ExecutionError("code candidate not found")
    try:
        ws = ws_mod.ensure_workspace(ctx.settings, cand.id)
    except ws_mod.WorkspaceError as exc:
        raise ExecutionError(f"workspace unavailable: {exc}") from exc
    return ws.path, cand


def _reads_in_correlation(ctx: ExecContext) -> int:
    return int(
        ctx.db.query(AgentIntent.id)
        .join(AgentRun, AgentRun.id == AgentIntent.run_id)
        .filter(
            AgentRun.correlation_id == ctx.run.correlation_id,
            AgentIntent.intent_type.in_([t.value for t in REPO_READ_INTENT_TYPES]),
            AgentIntent.execution_status == IntentExecutionStatus.EXECUTED,
        )
        .count()
    )


def _bytes_in_run(ctx: ExecContext) -> int:
    rows = (
        ctx.db.query(AgentIntent.result)
        .filter(AgentIntent.run_id == ctx.run.id, AgentIntent.execution_status == IntentExecutionStatus.EXECUTED, AgentIntent.intent_type.in_([t.value for t in REPO_READ_INTENT_TYPES]))
        .all()
    )
    total = 0
    for (res,) in rows:
        inner = (res or {}).get("result") or {}
        total += int(inner.get("bytes") or 0) if isinstance(inner, dict) else 0
    return total


def _check_read_bounds(ctx: ExecContext) -> None:
    s = ctx.settings
    if _reads_in_correlation(ctx) >= s.max_repo_reads_per_correlation:
        raise ExecutionError(f"repository read budget exhausted for this correlation ({s.max_repo_reads_per_correlation})")
    if _bytes_in_run(ctx) >= s.max_repo_bytes_per_run:
        raise ExecutionError(f"repository byte budget exhausted for this run ({s.max_repo_bytes_per_run} bytes)")


def _turns_used(ctx: ExecContext) -> int:
    return int(
        ctx.db.query(SocietyEvent.id)
        .filter(
            SocietyEvent.correlation_id == ctx.run.correlation_id,
            SocietyEvent.event_type == EventType.REPO_READ_RESULT,
            SocietyEvent.subject_type == "agent",
            SocietyEvent.subject_id == ctx.agent.id,
        )
        .count()
    )


def _correlation_turns(ctx: ExecContext) -> int:
    return int(
        ctx.db.query(SocietyEvent.id)
        .filter(SocietyEvent.correlation_id == ctx.run.correlation_id, SocietyEvent.event_type == EventType.REPO_READ_RESULT)
        .count()
    )


def _finish_read(ctx: ExecContext, result: repo_intel.ReadResult, cand: Optional[CodeCandidate], *, duplicate: bool = False) -> ExecOutcome:
    """Persist-able result + ONE targeted wake per run (idempotent) so the
    agent gets its next engineering turn — unless turns are exhausted or the
    read was a suppressed duplicate."""
    out = result.to_dict()
    out["duplicate"] = duplicate
    if cand is not None:
        cand.repo_reads = int(cand.repo_reads or 0) + 1
    events: List[str] = []
    s = ctx.settings
    if duplicate:
        out["note"] = "identical search already answered in this correlation; no new turn"
        return ExecOutcome(result=out, events=events)
    turns = _turns_used(ctx)
    if turns >= s.max_engineering_turns or _correlation_turns(ctx) >= s.max_correlation_engineering_turns or int(ctx.event.causation_depth or 0) + 1 > s.max_engineering_correlation_depth:
        out["turns_exhausted"] = True
        ev = emit_event(
            ctx.db,
            event_type=EventType.ENGINEERING_TURNS_EXHAUSTED,
            payload={"agent": ctx.agent.name, "turns_used": turns, "max": s.max_engineering_turns, "candidate_id": str(cand.id) if cand else None},
            actor_type="agent",
            actor_id=ctx.agent.id,
            correlation_id=ctx.run.correlation_id,
            idempotency_key=f"turns-exhausted:{ctx.run.correlation_id}:{ctx.agent.id}",
            source_run_id=ctx.run.id,
            notify=False,
        )
        events.append(str(ev.id))
        return ExecOutcome(result=out, events=events)
    preview = json_bounded(out.get("data") or {}, 1800)
    ev = emit_event(
        ctx.db,
        event_type=EventType.REPO_READ_RESULT,
        payload={
            "agent": ctx.agent.name,
            "op": result.op,
            "path": result.path,
            "candidate_id": str(cand.id) if cand else None,
            "intent_id": str(ctx.intent_row.id),
            "turn": turns + 1,
            "truncated": result.truncated,
            "preview": preview,
        },
        actor_type="agent",
        actor_id=ctx.agent.id,
        subject_type="agent",
        subject_id=ctx.agent.id,
        causation=ctx.event,
        idempotency_key=f"repo-read-result:{ctx.run.id}",
        source_run_id=ctx.run.id,
        trace_id=ctx.run.trace_id,
    )
    if cand is not None and not getattr(ev, "deduplicated", False):
        cand.engineering_turns = int(cand.engineering_turns or 0) + 1
    events.append(str(ev.id))
    return ExecOutcome(result=out, events=events)


def json_bounded(obj: Any, n: int) -> Any:
    import json as _json

    s = _json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)
    return _json.loads(s) if len(s) <= n else {"_truncated": True, "preview": s[: n - 1] + "…"}


def _list_repo_tree(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    _check_read_bounds(ctx)
    root, cand = _read_root(ctx, p.candidate_id)
    try:
        res = repo_intel.list_tree(root, p.path, depth=p.depth)
    except repo_intel.RepoReadError as exc:
        raise ExecutionError(f"read refused: {exc}") from exc
    return _finish_read(ctx, res, cand)


def _search_repo(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    _check_read_bounds(ctx)
    root, cand = _read_root(ctx, p.candidate_id)
    scope = str(p.candidate_id or "base")
    key = repo_intel.search_key(p.pattern, p.glob, p.regex, scope)
    prior = (
        ctx.db.query(AgentIntent)
        .join(AgentRun, AgentRun.id == AgentIntent.run_id)
        .filter(
            AgentRun.correlation_id == ctx.run.correlation_id,
            AgentIntent.agent_id == ctx.agent.id,
            AgentIntent.intent_type == IntentType.SEARCH_REPO.value,
            AgentIntent.execution_status == IntentExecutionStatus.EXECUTED,
            AgentIntent.id != ctx.intent_row.id,
        )
        .order_by(AgentIntent.executed_at.desc())
        .limit(20)
        .all()
    )
    for row in prior:
        inner = (row.result or {}).get("result") or {}
        if isinstance(inner, dict) and inner.get("search_key") == key:
            res = repo_intel.ReadResult(op="search", path=inner.get("path") or "", data=inner.get("data") or {}, truncated=bool(inner.get("truncated")), bytes_returned=0)
            outcome = _finish_read(ctx, res, cand, duplicate=True)
            outcome.result["search_key"] = key
            return outcome
    try:
        res = repo_intel.search(root, p.pattern, glob=p.glob, regex=p.regex, max_results=min(p.max_results, ctx.settings.max_search_results))
    except repo_intel.RepoReadError as exc:
        raise ExecutionError(f"search refused: {exc}") from exc
    outcome = _finish_read(ctx, res, cand)
    outcome.result["search_key"] = key
    return outcome


def _read_repo_file(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    _check_read_bounds(ctx)
    root, cand = _read_root(ctx, p.candidate_id)
    try:
        res = repo_intel.read_file(root, p.path, max_bytes=p.max_bytes)
    except repo_intel.RepoReadError as exc:
        raise ExecutionError(f"read refused: {exc}") from exc
    return _finish_read(ctx, res, cand)


def _read_repo_range(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    _check_read_bounds(ctx)
    root, cand = _read_root(ctx, p.candidate_id)
    try:
        res = repo_intel.read_range(root, p.path, p.start, p.end)
    except repo_intel.RepoReadError as exc:
        raise ExecutionError(f"read refused: {exc}") from exc
    return _finish_read(ctx, res, cand)


def _read_diff(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    _check_read_bounds(ctx)
    cand = ctx.db.query(CodeCandidate).filter(CodeCandidate.id == p.candidate_id).first()
    if cand is None:
        raise ExecutionError("code candidate not found")
    try:
        ws = ws_mod.ensure_workspace(ctx.settings, cand.id)
        diff = ws_mod.diff_text(ws)
    except ws_mod.WorkspaceError as exc:
        raise ExecutionError(f"workspace unavailable: {exc}") from exc
    res = repo_intel.diff_result(diff, list(cand.changed_files or []))
    return _finish_read(ctx, res, cand)


def _read_candidate_state(ctx: ExecContext) -> ExecOutcome:
    p = ctx.validated.payload
    cand = ctx.db.query(CodeCandidate).filter(CodeCandidate.id == p.candidate_id).first()
    if cand is None:
        raise ExecutionError("code candidate not found")
    promotions = ctx.db.query(CodePromotion).filter(CodePromotion.candidate_id == cand.id).order_by(CodePromotion.created_at.desc()).limit(3).all()
    experiments = ctx.db.query(ChangeExperiment).filter(ChangeExperiment.candidate_id == cand.id).order_by(ChangeExperiment.created_at.desc()).limit(3).all()
    data = {
        "candidate": {
            "id": str(cand.id),
            "title": cand.title,
            "status": _ev(cand.status),
            "risk_tier": cand.risk_tier,
            "branch": cand.branch_name,
            "base_sha": cand.base_sha,
            "head_sha": cand.head_sha,
            "changed_files": list(cand.changed_files or [])[:50],
            "diff_lines": cand.diff_lines,
            "qa": {k: (cand.qa_report or {}).get(k) for k in ("verdict", "summary", "attempts", "failures")},
            "security": {k: (cand.security_report or {}).get(k) for k in ("verdict", "findings", "static_findings")},
            "error": cand.error,
        },
        "promotions": [
            {"id": str(pr.id), "status": _ev(pr.status), "risk_tier": pr.risk_tier, "ci_state": pr.ci_state, "merge_state": pr.merge_state, "pr_number": pr.external_pr_number, "eligibility": pr.eligibility, "failure_reason": pr.failure_reason}
            for pr in promotions
        ],
        "experiments": [
            {"id": str(ex.id), "status": _ev(ex.status), "decision": ex.decision, "confidence": ex.confidence, "rollback_recommended": bool(ex.rollback_recommended), "hard_gates": ex.hard_gate_results, "metric_deltas": ex.metric_deltas}
            for ex in experiments
        ],
    }
    res = repo_intel.ReadResult(op="candidate_state", path=str(cand.id), data=json_bounded(data, 12000), bytes_returned=0)
    # State reads never spend an engineering turn: no wake event.
    out = res.to_dict()
    out["duplicate"] = False
    return ExecOutcome(result=out)


# ── Phase 8: A2A federation as a client (ADR-0009 D14) ─────────────────
# These handlers only RECORD a request (a pending ``a2a_outbound_calls`` row
# in this transaction). The federation pump (society/federation_pump.py)
# performs the network call and emits the result as an event whose payload is
# untrusted external data. The model never handles a URL it was not allowed,
# a credential, a raw card or a remote prompt.


def _a2a_request_event(ctx: ExecContext, call, event_type: str, payload: Dict[str, Any]) -> SocietyEvent:
    ev = _emit(ctx, event_type, payload, subject_type="a2a_outbound_call", subject_id=call.id)
    call.causation_id = ev.id
    call.correlation_id = ctx.run.correlation_id
    return ev


def _discover_a2a_agent(ctx: ExecContext) -> ExecOutcome:
    from ..a2a.orm import A2AOutboundCall

    p = ctx.validated.payload
    key = f"society:{ctx.intent_row.idempotency_key}"[:255]
    call = ctx.db.query(A2AOutboundCall).filter(A2AOutboundCall.idempotency_key == key).first()
    if call is None:
        call = A2AOutboundCall(
            id=uuid.uuid4(), initiator_class="society", initiator_id=ctx.agent.id, intent_id=ctx.intent_row.id,
            depth=1, operation="DiscoverAgent", idempotency_key=key, status="pending",
            result_summary={"request": {"cardUrl": p.card_url}},
        )
        ctx.db.add(call)
        ctx.db.flush()
    ev = _a2a_request_event(ctx, call, "a2a.discovery.requested", {"outbound_call_id": str(call.id), "reason": p.reason[:255]})
    return ExecOutcome(result={"outbound_call_id": str(call.id), "status": call.status}, events=[str(ev.id)])


def _refresh_a2a_agent(ctx: ExecContext) -> ExecOutcome:
    from ..a2a.orm import A2AOutboundCall, A2ARemoteAgent

    p = ctx.validated.payload
    if ctx.db.query(A2ARemoteAgent.id).filter(A2ARemoteAgent.id == p.remote_agent_id).first() is None:
        raise ExecutionError("unknown remote agent")
    key = f"society:{ctx.intent_row.idempotency_key}"[:255]
    call = ctx.db.query(A2AOutboundCall).filter(A2AOutboundCall.idempotency_key == key).first()
    if call is None:
        call = A2AOutboundCall(
            id=uuid.uuid4(), remote_agent_id=p.remote_agent_id, initiator_class="society", initiator_id=ctx.agent.id,
            intent_id=ctx.intent_row.id, depth=1, operation="RefreshAgent", idempotency_key=key, status="pending",
        )
        ctx.db.add(call)
        ctx.db.flush()
    ev = _a2a_request_event(ctx, call, "a2a.refresh.requested", {"outbound_call_id": str(call.id)})
    return ExecOutcome(result={"outbound_call_id": str(call.id), "status": call.status}, events=[str(ev.id)])


def _request_a2a_task(ctx: ExecContext) -> ExecOutcome:
    from sqlalchemy import func as _func

    from ..a2a.federation import client as a2a_client
    from ..a2a.orm import A2AOutboundCall

    p = ctx.validated.payload
    key = f"society:{ctx.intent_row.idempotency_key}"[:255]
    existing = ctx.db.query(A2AOutboundCall).filter(A2AOutboundCall.idempotency_key == key).first()
    if existing is None:
        day_ago = ctx.now - timedelta(days=1)
        society_calls = (
            ctx.db.query(_func.count(A2AOutboundCall.id))
            .filter(A2AOutboundCall.initiator_class == "society", A2AOutboundCall.operation == "SendMessage", A2AOutboundCall.created_at >= day_ago)
            .scalar() or 0
        )
        if society_calls >= ctx.settings.a2a_max_calls_per_day:
            raise ExecutionError(f"external spend budget: {society_calls} society A2A calls in 24h (A2A_SOCIETY_MAX_CALLS_PER_DAY)")
        in_correlation = (
            ctx.db.query(_func.count(A2AOutboundCall.id))
            .filter(A2AOutboundCall.correlation_id == ctx.run.correlation_id, A2AOutboundCall.operation == "SendMessage")
            .scalar() or 0
        )
        if in_correlation >= ctx.settings.a2a_max_calls_per_correlation:
            raise ExecutionError(f"agent-chain breaker: {in_correlation} A2A calls already in this correlation")
        from ..a2a.orm import A2AConnection

        conn = ctx.db.query(A2AConnection).filter(A2AConnection.id == p.connection_id).first()
        if conn is not None:
            failures = (
                ctx.db.query(_func.count(A2AOutboundCall.id))
                .filter(
                    A2AOutboundCall.remote_agent_id == conn.remote_agent_id,
                    A2AOutboundCall.status.in_(["failed", "timeout"]),
                    A2AOutboundCall.created_at >= ctx.now - timedelta(hours=1),
                )
                .scalar() or 0
            )
            if failures >= ctx.settings.a2a_breaker_failures:
                raise ExecutionError(f"circuit breaker open: {failures} failed calls to this remote agent in the last hour")
    req = a2a_client.OutboundRequest(
        connection_id=p.connection_id,
        skill_id=p.skill_id,
        input=p.input,
        idempotency_key=key,
        initiator_class="society",
        initiator_id=ctx.agent.id,
        correlation_id=ctx.run.correlation_id,
        intent_id=ctx.intent_row.id,
        depth=1,
    )
    try:
        call = a2a_client.prepare(ctx.db, req)
    except a2a_client.OutboundRefusedByPolicy as exc:
        raise ExecutionError(f"A2A request refused: {exc.reason}")
    ev = _a2a_request_event(
        ctx, call, "a2a.task.requested",
        {"outbound_call_id": str(call.id), "skill_id": p.skill_id, "budget_class": p.budget_class, "reason": p.reason[:255]},
    )
    return ExecOutcome(result={"outbound_call_id": str(call.id), "status": call.status}, events=[str(ev.id)])


def _check_a2a_task(ctx: ExecContext) -> ExecOutcome:
    from ..a2a.federation import client as a2a_client
    from ..a2a.orm import A2AOutboundCall

    p = ctx.validated.payload
    call = ctx.db.query(A2AOutboundCall).filter(A2AOutboundCall.id == p.outbound_call_id, A2AOutboundCall.initiator_class == "society").first()
    if call is None:
        raise ExecutionError("unknown outbound call")
    view = a2a_client.call_view(call)
    view.pop("connectionId", None)
    return ExecOutcome(result={"call": json_bounded(view, 4000)})


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
    IntentType.LIST_REPO_TREE: _list_repo_tree,
    IntentType.SEARCH_REPO: _search_repo,
    IntentType.READ_REPO_FILE: _read_repo_file,
    IntentType.READ_REPO_RANGE: _read_repo_range,
    IntentType.READ_DIFF: _read_diff,
    IntentType.READ_CANDIDATE_STATE: _read_candidate_state,
    IntentType.REQUEST_PR_PROMOTION: _request_pr_promotion,
    IntentType.REQUEST_MERGE_EVALUATION: _request_merge_evaluation,
    IntentType.REQUEST_STAGING_EVALUATION: _request_staging_evaluation,
    IntentType.RECORD_EVALUATION_RECOMMENDATION: _record_evaluation_recommendation,
    IntentType.DISCOVER_A2A_AGENT: _discover_a2a_agent,
    IntentType.REFRESH_A2A_AGENT: _refresh_a2a_agent,
    IntentType.REQUEST_A2A_TASK: _request_a2a_task,
    IntentType.CHECK_A2A_TASK: _check_a2a_task,
}


def execute(ctx: ExecContext) -> ExecOutcome:
    itype = ctx.validated.intent_type
    handler = HANDLERS.get(itype) if itype is not None else None
    if handler is None:
        raise ExecutionError(f"no executor for {ctx.validated.type_name} (fail closed)")
    return handler(ctx)
