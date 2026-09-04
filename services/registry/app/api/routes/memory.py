"""Memory item endpoints — durable lessons (society- or agent-scope).

A MemoryItem is a curated lesson the society wants to remember. Future
tasks read tagged memory before starting, breaking the
"agents repeat the same mistake" pattern. Memory complements (does NOT
duplicate) the spans/traces table: spans are the mechanical event log;
memory is the curated, human/agent-written summary with importance
scoring and tags.

Auth model:
- READS are public.
- WRITES require the user JWT.
- Society-scope writes (``scope=SOCIETY``, ``agent_id=null``) are
  open to any authenticated user; founder gating is handled in the
  dashboard UI rather than here so an agent can write society-wide
  lessons during reflection.
- Agent-scope writes (``scope=AGENT``) require the caller to own the
  target agent.
- DELETE: only the user who created the item (best-effort proxy:
  the user who owns the linked agent) can delete it.

The DB CHECK constraint ``chk_memory_scope_agent_consistency``
guarantees AGENT-scope rows always have an ``agent_id`` and SOCIETY
rows never do; the route surfaces that as a 400 if the body breaks
the rule before sending it down.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import cast, type_coerce
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from ...auth import get_current_user, get_current_user_or_agent
from ...authz import owns_agent, principal_agent_ids, require_operator_user
from ...database import get_db
from ...models import (
    Agent,
    MemoryItem,
    MemoryScope,
    TaskSession,
    User,
)

router = APIRouter()


# ── Pydantic schemas ────────────────────────────────────────────────────


class MemoryCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=240)
    content: str = Field(..., min_length=1)
    scope: MemoryScope = MemoryScope.SOCIETY
    agent_id: Optional[uuid.UUID] = None
    tags: List[str] = Field(default_factory=list)
    importance: int = Field(50, ge=0, le=100)
    source_task_id: Optional[uuid.UUID] = None

    @field_validator("title", "content")
    @classmethod
    def _strip_required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("field cannot be blank")
        return v

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, tags: List[str]) -> List[str]:
        # Lowercase + strip for predictable containment search.
        seen: set[str] = set()
        out: list[str] = []
        for t in tags:
            tt = t.strip().lower()
            if tt and tt not in seen:
                seen.add(tt)
                out.append(tt)
        return out


class MemoryOut(BaseModel):
    id: uuid.UUID
    agent_id: Optional[uuid.UUID]
    scope: MemoryScope
    title: str
    content: str
    tags: List[str]
    source_task_id: Optional[uuid.UUID]
    importance: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Helpers ─────────────────────────────────────────────────────────────


def _ensure_scope_agent_consistency(scope: MemoryScope, agent_id: Optional[uuid.UUID]) -> None:
    if scope == MemoryScope.AGENT and agent_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="AGENT-scope memory requires agent_id",
        )
    if scope == MemoryScope.SOCIETY and agent_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SOCIETY-scope memory must not carry agent_id",
        )


def _ensure_owner_for_agent_scope(
    db: Session,
    current_user: User,
    agent_id: Optional[uuid.UUID],
) -> None:
    if agent_id is None:
        return
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="agent not found")
    if agent.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cannot write agent-scope memory for an agent you do not own",
        )


def _apply_tag_filter(query, tag: Optional[str]):
    """Filter by tag containment using JSONB ``@>`` operator.

    The GIN index on ``memory_items.tags`` covers this query shape.
    Tags are normalized to lowercase on write, so we lowercase the
    incoming filter before matching.
    """
    if tag is None:
        return query
    needle = [tag.strip().lower()]
    return query.filter(
        cast(MemoryItem.tags, JSONB).op("@>")(type_coerce(needle, JSONB))
    )


# ── Routes ──────────────────────────────────────────────────────────────


@router.get("/", response_model=List[MemoryOut])
def list_memory(
    scope: Optional[MemoryScope] = Query(None),
    agent_id: Optional[uuid.UUID] = Query(None),
    tag: Optional[str] = Query(None, description="Match a single tag (case-insensitive)."),
    source_task_id: Optional[uuid.UUID] = Query(None),
    min_importance: int = Query(0, ge=0, le=100),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current=Depends(get_current_user_or_agent),
):
    """List memory items visible to the principal: SOCIETY-scope lessons plus
    AGENT-scope lessons of agents it owns. Filters compose with AND."""
    mine = principal_agent_ids(db, current)
    visible = MemoryItem.scope == MemoryScope.SOCIETY.value
    if mine:
        visible = visible | MemoryItem.agent_id.in_(mine)
    query = db.query(MemoryItem).filter(visible)
    if scope:
        query = query.filter(MemoryItem.scope == scope.value)
    if agent_id:
        query = query.filter(MemoryItem.agent_id == agent_id)
    if source_task_id:
        query = query.filter(MemoryItem.source_task_id == source_task_id)
    if min_importance > 0:
        query = query.filter(MemoryItem.importance >= min_importance)
    query = _apply_tag_filter(query, tag)

    return (
        query.order_by(MemoryItem.importance.desc(), MemoryItem.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get("/{memory_id}", response_model=MemoryOut)
def get_memory(memory_id: uuid.UUID, db: Session = Depends(get_db), current=Depends(get_current_user_or_agent)):
    item = db.query(MemoryItem).filter(MemoryItem.id == memory_id).first()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="memory item not found")
    scope = item.scope.value if hasattr(item.scope, "value") else item.scope
    if scope != MemoryScope.SOCIETY.value and not (item.agent_id and owns_agent(db, current, item.agent_id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="memory item not found")
    return item


@router.post("/", response_model=MemoryOut, status_code=status.HTTP_201_CREATED)
def create_memory(
    payload: MemoryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Write a lesson. Society-scope lessons are read by every agent before
    it acts, so writing them is an operator action; agent-scope requires
    ownership of the target agent."""
    _ensure_scope_agent_consistency(payload.scope, payload.agent_id)
    if payload.scope == MemoryScope.SOCIETY:
        require_operator_user(current_user, detail="society-scope memory is written by operators (or the runtime)")
    _ensure_owner_for_agent_scope(db, current_user, payload.agent_id)

    if payload.source_task_id is not None:
        task = db.query(TaskSession).filter(TaskSession.id == payload.source_task_id).first()
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="source_task_id does not reference an existing task",
            )

    item = MemoryItem(
        id=uuid.uuid4(),
        agent_id=payload.agent_id,
        scope=payload.scope,
        title=payload.title,
        content=payload.content,
        tags=payload.tags,
        source_task_id=payload.source_task_id,
        importance=payload.importance,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory(
    memory_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Hard delete. Permission proxy: only the owner of the linked
    agent can delete agent-scope items; society-scope items cannot be
    deleted via the API (audit trail discipline)."""
    item = db.query(MemoryItem).filter(MemoryItem.id == memory_id).first()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="memory item not found")

    if item.agent_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="society-scope memory cannot be deleted via API",
        )
    agent = db.query(Agent).filter(Agent.id == item.agent_id).first()
    if agent is None or agent.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only the owning user can delete agent-scope memory",
        )

    db.delete(item)
    db.commit()
    return None
