"""Goal endpoints — agent missions and structured objectives.

Every agent has a primary `mission` text plus 0..N active `Goal`s with
priority, success criteria, and a parent/child tree (epic -> milestone
-> goal). Tasks may declare which goal they advance; the routes here
expose CRUD + lifecycle transitions plus an agent-scoped view at
``GET /v1/agents/{agent_id}/goals``.

Auth model:
- READS are public (anyone can browse the society's mission map).
- WRITES require the user JWT and that the user owns the goal:
  - ``owner_type=USER``: ``owner_id == current_user.id``
  - ``owner_type=AGENT``: agent must be one of ``current_user.agents``
  - ``owner_type=SOCIETY``: any authenticated user can write (founder-gated
    in production via the dashboard, not here).

State machine:
    active <-> paused
    active -> completed | failed | cancelled (terminal)
    paused -> active | cancelled
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...database import get_db
from ...models import (
    Agent,
    Goal,
    GoalOwnerType,
    GoalPriority,
    GoalStatus,
    TaskSession,
    User,
)

router = APIRouter()


# ── State machine ────────────────────────────────────────────────────────


_GOAL_TRANSITIONS: dict[GoalStatus, set[GoalStatus]] = {
    GoalStatus.ACTIVE: {GoalStatus.PAUSED, GoalStatus.COMPLETED, GoalStatus.FAILED, GoalStatus.CANCELLED},
    GoalStatus.PAUSED: {GoalStatus.ACTIVE, GoalStatus.CANCELLED},
    GoalStatus.COMPLETED: set(),
    GoalStatus.FAILED: set(),
    GoalStatus.CANCELLED: set(),
}


def _check_transition(current: GoalStatus, target: GoalStatus) -> None:
    if target == current:
        return
    if target not in _GOAL_TRANSITIONS.get(current, set()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"goal cannot transition {current.value} -> {target.value}",
        )


def _ensure_owner(db: Session, current_user: User, owner_type: GoalOwnerType, owner_id: uuid.UUID) -> None:
    if owner_type == GoalOwnerType.SOCIETY:
        return
    if owner_type == GoalOwnerType.USER:
        if owner_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="cannot create/edit USER-scoped goal for another user",
            )
        return
    if owner_type == GoalOwnerType.AGENT:
        agent = db.query(Agent).filter(Agent.id == owner_id).first()
        if agent is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="owner agent not found")
        if agent.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="cannot create/edit goal for an agent you do not own",
            )


# ── Pydantic schemas ────────────────────────────────────────────────────


class GoalCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=240)
    description: Optional[str] = None
    owner_type: GoalOwnerType
    owner_id: uuid.UUID
    priority: GoalPriority = GoalPriority.MEDIUM
    success_criteria: List[str] = Field(default_factory=list)
    parent_goal_id: Optional[uuid.UUID] = None
    target_date: Optional[datetime] = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("title cannot be empty")
        return v


class GoalUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=240)
    description: Optional[str] = None
    priority: Optional[GoalPriority] = None
    status: Optional[GoalStatus] = None
    success_criteria: Optional[List[str]] = None
    parent_goal_id: Optional[uuid.UUID] = None
    target_date: Optional[datetime] = None


class GoalOut(BaseModel):
    id: uuid.UUID
    title: str
    description: Optional[str] = None
    owner_type: GoalOwnerType
    owner_id: uuid.UUID
    priority: GoalPriority
    status: GoalStatus
    success_criteria: List[str]
    parent_goal_id: Optional[uuid.UUID] = None
    target_date: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    execution_mode: str = "legacy"

    model_config = {"from_attributes": True}


class GoalDetail(BaseModel):
    goal: GoalOut
    children: List[GoalOut]
    related_tasks: int


# ── Routes ──────────────────────────────────────────────────────────────


@router.get("/", response_model=List[GoalOut])
def list_goals(
    status_filter: Optional[GoalStatus] = Query(None, alias="status"),
    owner_type: Optional[GoalOwnerType] = Query(None),
    owner_id: Optional[uuid.UUID] = Query(None),
    priority: Optional[GoalPriority] = Query(None),
    parent_goal_id: Optional[uuid.UUID] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """List goals with optional filters. Public — no auth."""
    query = db.query(Goal)
    if status_filter:
        query = query.filter(Goal.status == status_filter.value)
    if owner_type:
        query = query.filter(Goal.owner_type == owner_type.value)
    if owner_id:
        query = query.filter(Goal.owner_id == owner_id)
    if priority:
        query = query.filter(Goal.priority == priority.value)
    if parent_goal_id:
        query = query.filter(Goal.parent_goal_id == parent_goal_id)
    return query.order_by(Goal.created_at.desc()).offset(skip).limit(limit).all()


@router.post("/", response_model=GoalOut, status_code=status.HTTP_201_CREATED)
def create_goal(
    payload: GoalCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a goal. Requires ownership of the target."""
    _ensure_owner(db, current_user, payload.owner_type, payload.owner_id)

    if payload.parent_goal_id:
        parent = db.query(Goal).filter(Goal.id == payload.parent_goal_id).first()
        if parent is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="parent goal not found")

    if payload.target_date and payload.target_date < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="target_date cannot be in the past",
        )

    goal = Goal(
        id=uuid.uuid4(),
        title=payload.title,
        description=payload.description,
        owner_type=payload.owner_type,
        owner_id=payload.owner_id,
        priority=payload.priority,
        status=GoalStatus.ACTIVE,
        success_criteria=payload.success_criteria,
        parent_goal_id=payload.parent_goal_id,
        target_date=payload.target_date,
        execution_mode="legacy",
    )
    db.add(goal)
    db.commit()
    db.refresh(goal)
    return goal


@router.get("/{goal_id}", response_model=GoalDetail)
def get_goal(goal_id: uuid.UUID, db: Session = Depends(get_db)):
    """Get goal detail including children and a count of related tasks."""
    goal = db.query(Goal).filter(Goal.id == goal_id).first()
    if goal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="goal not found")

    children = db.query(Goal).filter(Goal.parent_goal_id == goal_id).all()
    # Count tasks where the agent's current_goal_id matches this goal — a
    # cheap proxy for "tasks advancing this goal" until task_sessions
    # gains its own goal_id column in a later migration.
    related_tasks = (
        db.query(TaskSession)
        .join(Agent, Agent.id == TaskSession.callee_agent_id)
        .filter(Agent.current_goal_id == goal_id)
        .count()
    )
    return GoalDetail(goal=goal, children=children, related_tasks=related_tasks)


@router.patch("/{goal_id}", response_model=GoalOut)
def update_goal(
    goal_id: uuid.UUID,
    payload: GoalUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update goal fields and/or transition status."""
    goal = db.query(Goal).filter(Goal.id == goal_id).first()
    if goal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="goal not found")

    _ensure_owner(db, current_user, goal.owner_type, goal.owner_id)

    update_fields = payload.model_dump(exclude_unset=True)

    if "status" in update_fields:
        new_status = update_fields["status"]
        if isinstance(new_status, str):
            new_status = GoalStatus(new_status)
        _check_transition(goal.status if isinstance(goal.status, GoalStatus) else GoalStatus(goal.status), new_status)
        if new_status == GoalStatus.COMPLETED:
            goal.completed_at = datetime.now(timezone.utc)

    if "parent_goal_id" in update_fields and update_fields["parent_goal_id"]:
        if update_fields["parent_goal_id"] == goal.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="goal cannot be its own parent")
        # Cycle defence: at most a one-hop check (A->B; reject if B's
        # parent is A). Deeper cycles are caught by tree traversal at
        # query time but not here — acceptable for v1.
        candidate_parent = (
            db.query(Goal).filter(Goal.id == update_fields["parent_goal_id"]).first()
        )
        if candidate_parent is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="parent goal not found",
            )
        if candidate_parent.parent_goal_id == goal.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="parent goal would create a cycle",
            )

    if "target_date" in update_fields and update_fields["target_date"] is not None:
        # Same rule as create: target_date must not be in the past.
        # If a caller wants to reschedule a missed deadline, they should
        # transition the goal to FAILED + cancel + create a fresh one.
        if update_fields["target_date"] < datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="target_date cannot be in the past",
            )

    for key, value in update_fields.items():
        setattr(goal, key, value)

    db.commit()
    db.refresh(goal)
    return goal


@router.post("/{goal_id}/complete", response_model=GoalOut)
def complete_goal(
    goal_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Shortcut for status -> completed."""
    goal = db.query(Goal).filter(Goal.id == goal_id).first()
    if goal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="goal not found")
    _ensure_owner(db, current_user, goal.owner_type, goal.owner_id)
    current = goal.status if isinstance(goal.status, GoalStatus) else GoalStatus(goal.status)
    _check_transition(current, GoalStatus.COMPLETED)
    goal.status = GoalStatus.COMPLETED
    goal.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(goal)
    return goal


@router.post("/{goal_id}/fail", response_model=GoalOut)
def fail_goal(
    goal_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Shortcut for status -> failed."""
    goal = db.query(Goal).filter(Goal.id == goal_id).first()
    if goal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="goal not found")
    _ensure_owner(db, current_user, goal.owner_type, goal.owner_id)
    current = goal.status if isinstance(goal.status, GoalStatus) else GoalStatus(goal.status)
    _check_transition(current, GoalStatus.FAILED)
    goal.status = GoalStatus.FAILED
    db.commit()
    db.refresh(goal)
    return goal


@router.delete("/{goal_id}", response_model=GoalOut)
def cancel_goal(
    goal_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Soft-delete: status -> cancelled. Hard delete is not exposed."""
    goal = db.query(Goal).filter(Goal.id == goal_id).first()
    if goal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="goal not found")
    _ensure_owner(db, current_user, goal.owner_type, goal.owner_id)
    current = goal.status if isinstance(goal.status, GoalStatus) else GoalStatus(goal.status)
    _check_transition(current, GoalStatus.CANCELLED)
    goal.status = GoalStatus.CANCELLED
    db.commit()
    db.refresh(goal)
    return goal
