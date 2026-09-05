"""Improvement proposal endpoints — the spine of the self-improvement loop.

When a task fails, when a review rejects, when QA flags an issue, the
platform produces a structured proposal: problem, root cause, proposed
change, expected benefit, risk, status. Proposals can be approved ->
converted to a real Task, then ultimately marked Implemented when that
task ships (status == 'completed').

Lifecycle:
    PROPOSED -> UNDER_REVIEW -> APPROVED -> CONVERTED_TO_TASK -> IMPLEMENTED
                              `-> REJECTED (terminal)

Auth model:
- READ is public.
- POST creates as the calling user/agent.
- approve/reject require any authenticated user (founder gates via
  dashboard UI; tightening here is a backlog item).
- approve guard: caller cannot equal proposed_by_user_id or proposed_by_agent_id.
- convert-to-task creates a TaskSession via the existing task pipeline
  with proper escrow checks (we do NOT bypass escrow). The endpoint
  defers to the standard /v1/tasks creation path.
- mark-implemented requires the converted_task_id to be in status 'completed'.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ...auth import get_current_user, get_current_user_or_agent
from ...authz import require_operator_user, require_party, user_is_operator
from ...task_service import EscrowError, create_task_with_escrow
from ...database import get_db
from ...models import (
    Agent,
    CurrencyType,
    ImprovementProposal,
    ProposalScope,
    ProposalSource,
    ProposalStatus,
    TaskSession,
    TaskStatus,
    User,
    Wallet,
    WalletOwnerType,
)
from ...reflection import reflect_on_task

router = APIRouter()


# ── State machine ────────────────────────────────────────────────────────


_PROPOSAL_TRANSITIONS: dict[ProposalStatus, set[ProposalStatus]] = {
    ProposalStatus.PROPOSED: {
        ProposalStatus.UNDER_REVIEW,
        ProposalStatus.APPROVED,
        ProposalStatus.REJECTED,
    },
    ProposalStatus.UNDER_REVIEW: {
        ProposalStatus.APPROVED,
        ProposalStatus.REJECTED,
    },
    ProposalStatus.APPROVED: {
        ProposalStatus.CONVERTED_TO_TASK,
    },
    ProposalStatus.CONVERTED_TO_TASK: {
        ProposalStatus.IMPLEMENTED,
    },
    ProposalStatus.REJECTED: set(),
    ProposalStatus.IMPLEMENTED: set(),
}


def _check_transition(current: ProposalStatus, target: ProposalStatus) -> None:
    if target == current:
        return
    if target not in _PROPOSAL_TRANSITIONS.get(current, set()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"proposal cannot transition {current.value} -> {target.value}",
        )


def _coerce_status(raw: object) -> ProposalStatus:
    if isinstance(raw, ProposalStatus):
        return raw
    return ProposalStatus(str(raw))


# ── Pydantic schemas ────────────────────────────────────────────────────


class ProposalCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=240)
    source: ProposalSource = ProposalSource.HUMAN_FEEDBACK
    problem: Optional[str] = None
    root_cause: Optional[str] = None
    proposed_change: Optional[str] = None
    expected_benefit: Optional[str] = None
    risk: Optional[str] = None
    target_scope: ProposalScope = ProposalScope.AGENT
    importance: int = Field(50, ge=0, le=100)
    source_task_id: Optional[uuid.UUID] = None
    on_behalf_of_agent_id: Optional[uuid.UUID] = Field(
        None,
        description="If set, the proposal is attributed to this agent (must be owned by the caller).",
    )

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("title cannot be empty")
        return v


class ProposalOut(BaseModel):
    id: uuid.UUID
    proposed_by_agent_id: Optional[uuid.UUID]
    proposed_by_user_id: Optional[uuid.UUID]
    source: ProposalSource
    title: str
    problem: Optional[str]
    root_cause: Optional[str]
    proposed_change: Optional[str]
    expected_benefit: Optional[str]
    risk: Optional[str]
    status: ProposalStatus
    target_scope: ProposalScope
    importance: int
    source_task_id: Optional[uuid.UUID]
    converted_task_id: Optional[uuid.UUID]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RejectBody(BaseModel):
    reason: Optional[str] = None


class ConvertBody(BaseModel):
    callee_agent_id: uuid.UUID = Field(
        ...,
        description="Agent that will execute the resulting task. The caller-side agent is auto-derived from the proposed_by_agent_id (or the first agent owned by the user).",
    )
    capability: str = Field(..., min_length=1, max_length=120)
    escrow_amount: int = Field(..., ge=0)
    timeout_minutes: int = Field(60, ge=1, le=24 * 60)
    input: dict = Field(default_factory=dict)


class ReflectBody(BaseModel):
    task_id: uuid.UUID
    persist: bool = Field(
        False,
        description="If true, save the reflection as a proposal. If false (default), return the preview payload only.",
    )


# ── Routes ──────────────────────────────────────────────────────────────


@router.get("/", response_model=List[ProposalOut])
def list_proposals(
    status_filter: Optional[ProposalStatus] = Query(None, alias="status"),
    source: Optional[ProposalSource] = Query(None),
    agent_id: Optional[uuid.UUID] = Query(None),
    user_id: Optional[uuid.UUID] = Query(None),
    target_scope: Optional[ProposalScope] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _current=Depends(get_current_user_or_agent),
):
    """List improvement proposals (authenticated: they describe platform
    problems, root causes and risks)."""
    query = db.query(ImprovementProposal)
    if status_filter:
        query = query.filter(ImprovementProposal.status == status_filter.value)
    if source:
        query = query.filter(ImprovementProposal.source == source.value)
    if agent_id:
        query = query.filter(ImprovementProposal.proposed_by_agent_id == agent_id)
    if user_id:
        query = query.filter(ImprovementProposal.proposed_by_user_id == user_id)
    if target_scope:
        query = query.filter(ImprovementProposal.target_scope == target_scope.value)
    return (
        query.order_by(ImprovementProposal.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get("/{proposal_id}", response_model=ProposalOut)
def get_proposal(proposal_id: uuid.UUID, db: Session = Depends(get_db), _current=Depends(get_current_user_or_agent)):
    proposal = (
        db.query(ImprovementProposal).filter(ImprovementProposal.id == proposal_id).first()
    )
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found")
    return proposal


@router.post("/", response_model=ProposalOut, status_code=status.HTTP_201_CREATED)
def create_proposal(
    payload: ProposalCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a proposal manually. Caller is recorded as proposed_by_user_id;
    if ``on_behalf_of_agent_id`` is set, the agent is attributed instead
    and must be owned by the caller."""
    proposed_by_agent_id: Optional[uuid.UUID] = None
    if payload.on_behalf_of_agent_id is not None:
        agent = db.query(Agent).filter(Agent.id == payload.on_behalf_of_agent_id).first()
        if agent is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="on_behalf_of agent not found")
        if agent.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="cannot propose on behalf of an agent you do not own",
            )
        proposed_by_agent_id = agent.id

    if payload.source_task_id is not None:
        task = db.query(TaskSession).filter(TaskSession.id == payload.source_task_id).first()
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="source_task_id does not reference an existing task",
            )

    proposal = ImprovementProposal(
        id=uuid.uuid4(),
        proposed_by_agent_id=proposed_by_agent_id,
        proposed_by_user_id=current_user.id,
        source=payload.source,
        title=payload.title,
        problem=payload.problem,
        root_cause=payload.root_cause,
        proposed_change=payload.proposed_change,
        expected_benefit=payload.expected_benefit,
        risk=payload.risk,
        status=ProposalStatus.PROPOSED,
        target_scope=payload.target_scope,
        importance=payload.importance,
        source_task_id=payload.source_task_id,
    )
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    return proposal


@router.post("/{proposal_id}/approve", response_model=ProposalOut)
def approve_proposal(
    proposal_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Approve a proposal (operator role). Self-approval guard: caller cannot
    equal proposed_by_user_id, and cannot own the proposed_by_agent."""
    require_operator_user(current_user, detail="approving proposals requires the operator role")
    proposal = (
        db.query(ImprovementProposal).filter(ImprovementProposal.id == proposal_id).first()
    )
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found")

    if proposal.proposed_by_user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cannot approve your own proposal",
        )
    if proposal.proposed_by_agent_id is not None:
        agent = db.query(Agent).filter(Agent.id == proposal.proposed_by_agent_id).first()
        if agent is not None and agent.user_id == current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="cannot approve a proposal from an agent you own",
            )

    _check_transition(_coerce_status(proposal.status), ProposalStatus.APPROVED)
    proposal.status = ProposalStatus.APPROVED
    db.commit()
    db.refresh(proposal)
    return proposal


@router.post("/{proposal_id}/reject", response_model=ProposalOut)
def reject_proposal(
    proposal_id: uuid.UUID,
    body: RejectBody = RejectBody(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reject a proposal: operators, or the user who proposed it."""
    proposal = (
        db.query(ImprovementProposal).filter(ImprovementProposal.id == proposal_id).first()
    )
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found")
    if proposal.proposed_by_user_id != current_user.id and not user_is_operator(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="only the proposer or an operator can reject a proposal")

    _check_transition(_coerce_status(proposal.status), ProposalStatus.REJECTED)
    proposal.status = ProposalStatus.REJECTED
    if body.reason:
        # Append to risk field so the rejection rationale is queryable.
        existing = proposal.risk or ""
        proposal.risk = (existing + "\n[REJECTED] " + body.reason).strip()
    db.commit()
    db.refresh(proposal)
    return proposal


@router.post("/{proposal_id}/convert-to-task", response_model=ProposalOut)
def convert_proposal_to_task(
    proposal_id: uuid.UUID,
    body: ConvertBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Promote an APPROVED proposal into a real ``TaskSession``.

    Picks the caller-side agent: if ``proposed_by_agent_id`` is set and
    the caller owns that agent, use it. Otherwise pick the caller's
    first owned agent. Wallet/escrow check delegates to the standard
    task creation invariants — we read reserved_credits + balance_credits
    here to short-circuit, but the actual escrow lock happens via the
    standard /v1/tasks pipeline once that agent submits via WS/REST.
    For v1 the conversion just creates the task row; the caller agent
    can then START it via the existing flow.
    """
    require_operator_user(current_user, detail="converting proposals into paid tasks requires the operator role")
    proposal = (
        db.query(ImprovementProposal).filter(ImprovementProposal.id == proposal_id).first()
    )
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found")
    _check_transition(_coerce_status(proposal.status), ProposalStatus.CONVERTED_TO_TASK)

    caller_agent: Optional[Agent] = None
    if proposal.proposed_by_agent_id is not None:
        candidate = db.query(Agent).filter(Agent.id == proposal.proposed_by_agent_id).first()
        if candidate is not None and candidate.user_id == current_user.id:
            caller_agent = candidate
    if caller_agent is None:
        caller_agent = (
            db.query(Agent).filter(Agent.user_id == current_user.id).order_by(Agent.created_at.asc()).first()
        )
    if caller_agent is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="caller has no owned agent to use as task initiator",
        )

    callee = db.query(Agent).filter(Agent.id == body.callee_agent_id).first()
    if callee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="callee agent not found")
    if caller_agent.id == callee.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="caller and callee must be distinct",
        )

    caller_wallet = (
        db.query(Wallet)
        .filter(
            Wallet.owner_type == WalletOwnerType.AGENT,
            Wallet.owner_id == caller_agent.id,
        )
        .first()
    )
    if caller_wallet is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="caller agent has no wallet",
        )
    available = (caller_wallet.balance_credits or 0) - (caller_wallet.reserved_credits or 0)
    if available < body.escrow_amount:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"insufficient available credits: have {available}, need {body.escrow_amount}",
        )

    # The task goes through the ONE escrow path (task_service): funds are
    # reserved on the caller wallet now, so a later completion/refund can
    # never release money that was never locked.
    try:
        task, _tx = create_task_with_escrow(
            db=db,
            caller_agent=caller_agent,
            callee_agent_id=callee.id,
            capability_name=body.capability,
            input_data=body.input or {},
            max_budget=body.escrow_amount,
            currency="credits",
            timeout_seconds=body.timeout_minutes * 60,
            idempotency_key=f"proposal:{proposal.id}",
        )
    except EscrowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    proposal.status = ProposalStatus.CONVERTED_TO_TASK
    proposal.converted_task_id = task.id
    db.commit()
    db.refresh(proposal)
    return proposal


@router.post("/{proposal_id}/mark-implemented", response_model=ProposalOut)
def mark_proposal_implemented(
    proposal_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Promote CONVERTED_TO_TASK -> IMPLEMENTED (operator role). Requires the linked task
    to be in 'completed' status."""
    require_operator_user(current_user, detail="marking proposals implemented requires the operator role")
    proposal = (
        db.query(ImprovementProposal).filter(ImprovementProposal.id == proposal_id).first()
    )
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found")

    _check_transition(_coerce_status(proposal.status), ProposalStatus.IMPLEMENTED)

    if proposal.converted_task_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="proposal has no linked task to verify",
        )
    task = db.query(TaskSession).filter(TaskSession.id == proposal.converted_task_id).first()
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="linked task no longer exists",
        )
    task_status = task.status if isinstance(task.status, str) else task.status.value
    if task_status != TaskStatus.COMPLETED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"linked task must be completed (current: {task_status})",
        )

    proposal.status = ProposalStatus.IMPLEMENTED
    db.commit()
    db.refresh(proposal)
    return proposal


@router.post("/reflect")
def reflect_endpoint(
    body: ReflectBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Auto-generate an improvement proposal from a (typically failed) task.

    With ``persist=false`` (default) the response is just the preview
    payload — the dashboard can show it before saving. With
    ``persist=true`` the proposal is created and the row is returned.
    """
    task = db.query(TaskSession).filter(TaskSession.id == body.task_id).first()
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    if not user_is_operator(current_user):
        require_party(db, current_user, [task.caller_agent_id, task.callee_agent_id], detail="you can only reflect on tasks your agents took part in")

    payload = reflect_on_task(task)
    payload_out = {
        **payload,
        "source": payload["source"].value if hasattr(payload["source"], "value") else payload["source"],
        "target_scope": payload["target_scope"].value
        if hasattr(payload["target_scope"], "value")
        else payload["target_scope"],
        "source_task_id": str(task.id),
    }

    if not body.persist:
        return {"preview": payload_out}

    # Idempotency: if a proposal already exists for this task, return it.
    existing = (
        db.query(ImprovementProposal)
        .filter(ImprovementProposal.source_task_id == task.id)
        .order_by(ImprovementProposal.created_at.desc())
        .first()
    )
    if existing is not None:
        return {"preview": payload_out, "proposal_id": str(existing.id), "created": False}

    # Attribute to the callee agent if known, else the user.
    proposed_by_agent_id = task.callee_agent_id or task.caller_agent_id
    proposal = ImprovementProposal(
        id=uuid.uuid4(),
        proposed_by_agent_id=proposed_by_agent_id,
        proposed_by_user_id=current_user.id,
        source=payload["source"],
        title=payload["title"],
        problem=payload["problem"],
        root_cause=payload["root_cause"],
        proposed_change=payload["proposed_change"],
        expected_benefit=payload["expected_benefit"],
        risk=payload["risk"],
        target_scope=payload["target_scope"],
        importance=payload["importance"],
        source_task_id=task.id,
        status=ProposalStatus.PROPOSED,
    )
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    return {"preview": payload_out, "proposal_id": str(proposal.id), "created": True}
