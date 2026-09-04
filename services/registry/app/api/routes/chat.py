import logging
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import UUID4, BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...auth import get_current_agent, get_current_user, get_current_user_or_agent
from ...authz import owns_agent, principal_agent_ids
from ...database import get_db
from ...models import Agent, AgentChat, AgentMessageType, User

logger = logging.getLogger(__name__)

router = APIRouter()


# ─── Schemas ───────────────────────────────────────────────

class ChatMessageResponse(BaseModel):
    id: UUID4
    from_agent_id: UUID4
    to_agent_id: Optional[UUID4] = None
    message_type: str
    title: str
    content: str
    thread_id: Optional[UUID4] = None
    is_read: bool
    created_at: datetime
    # Extra fields (not in DB)
    from_agent_name: Optional[str] = None
    to_agent_name: Optional[str] = None

    class Config:
        from_attributes = True


class ChatMessageCreate(BaseModel):
    to_agent_id: Optional[UUID4] = None
    message_type: str = "note"
    title: str
    content: str
    metadata: dict = {}
    thread_id: Optional[UUID4] = None
    from_agent_name: Optional[str] = None


class ChatThread(BaseModel):
    thread_id: UUID4
    messages: List[ChatMessageResponse]
    latest_at: datetime
    message_count: int


# ─── Endpoints ─────────────────────────────────────────────

@router.post("/", response_model=ChatMessageResponse, status_code=status.HTTP_201_CREATED)
async def send_message(
    msg: ChatMessageCreate,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user_or_agent),
):
    """Send a message from one agent to another (or broadcast to all)."""

    # Validate message_type
    try:
        msg_type = AgentMessageType(msg.message_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid message_type: {msg.message_type}")

    # If to_agent_id is set, verify it exists
    if msg.to_agent_id:
        target = db.query(Agent).filter(Agent.id == msg.to_agent_id).first()
        if not target:
            raise HTTPException(status_code=404, detail="Target agent not found")

    # The sender is always an agent the principal controls: an agent token
    # sends as itself; a user must name one of their OWN agents. Nobody can
    # impersonate another agent by naming it.
    if isinstance(current, Agent):
        agent_obj = current
    else:
        if not msg.from_agent_name:
            raise HTTPException(status_code=422, detail="from_agent_name is required: users send as one of their own agents")
        agent_obj = db.query(Agent).filter(Agent.name == msg.from_agent_name, Agent.user_id == current.id).first()
        if agent_obj is None:
            raise HTTPException(status_code=403, detail="from_agent_name must be an agent you own")
    agent_name = agent_obj.name

    from_agent_id = agent_obj.id

    # Create message
    db_msg = AgentChat(
        id=uuid.uuid4(),
        from_agent_id=from_agent_id,
        to_agent_id=msg.to_agent_id,
        message_type=msg_type,
        title=msg.title,
        content=msg.content,
        msg_metadata=msg.metadata,
        thread_id=msg.thread_id or uuid.uuid4(),
        is_read=False,
    )
    db.add(db_msg)
    db.commit()
    db.refresh(db_msg)

    # Broadcast to WebSocket
    from ...websocket_manager import manager
    await manager.broadcast({
        "type": "agent_chat",
        "from_agent": str(agent_obj.id),
        "from_name": agent_obj.name,
        "to_agent": str(msg.to_agent_id) if msg.to_agent_id else None,
        "message_type": msg.message_type,
        "title": msg.title,
        "thread_id": str(db_msg.thread_id),
        "id": str(db_msg.id),
        "created_at": db_msg.created_at.isoformat(),
    })

    logger.info(f"Agent chat: {agent_obj.name} → {msg.to_agent_id or 'ALL'} [{msg.message_type}] {msg.title}")
    return db_msg


@router.get("/", response_model=List[ChatMessageResponse])
async def list_messages(
    agent_id: Optional[UUID4] = Query(None, description="Filter by agent (from or to)"),
    message_type: Optional[str] = Query(None),
    thread_id: Optional[UUID4] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user_or_agent),
):
    """List chat messages the principal is a party to: sent by or addressed
    to one of its agents, or broadcast (no recipient)."""
    mine = principal_agent_ids(db, current)
    query = db.query(AgentChat).filter(
        AgentChat.from_agent_id.in_(mine) | AgentChat.to_agent_id.in_(mine) | AgentChat.to_agent_id.is_(None)
    ) if mine else db.query(AgentChat).filter(AgentChat.to_agent_id.is_(None))

    if agent_id:
        query = query.filter(
            (AgentChat.from_agent_id == agent_id) | (AgentChat.to_agent_id == agent_id)
        )
    if message_type:
        query = query.filter(AgentChat.message_type == message_type)
    if thread_id:
        query = query.filter(AgentChat.thread_id == thread_id)

    messages = query.order_by(AgentChat.created_at.desc()).offset(offset).limit(limit).all()

    # Enrich with agent names
    result = []
    for m in messages:
        d = m.__dict__.copy()
        from_agent = db.query(Agent).filter(Agent.id == m.from_agent_id).first()
        d["from_agent_name"] = from_agent.name if from_agent else "unknown"
        if m.to_agent_id:
            to_agent = db.query(Agent).filter(Agent.id == m.to_agent_id).first()
            d["to_agent_name"] = to_agent.name if to_agent else "unknown"
        result.append(ChatMessageResponse.model_validate(d))

    return result


@router.get("/threads", response_model=List[ChatThread])
async def list_threads(
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user_or_agent),
):
    """List conversation threads the principal is a party to (authenticated)."""
    mine = principal_agent_ids(db, current)
    visible = AgentChat.from_agent_id.in_(mine) | AgentChat.to_agent_id.in_(mine) | AgentChat.to_agent_id.is_(None) if mine else AgentChat.to_agent_id.is_(None)
    # Get distinct thread IDs
    thread_ids = (
        db.query(AgentChat.thread_id, func.max(AgentChat.created_at).label("latest"))
        .filter(visible)
        .group_by(AgentChat.thread_id)
        .order_by(func.max(AgentChat.created_at).desc())
        .limit(limit)
        .all()
    )

    threads = []
    for tid, _ in thread_ids:
        messages = (
            db.query(AgentChat)
            .filter(AgentChat.thread_id == tid, visible)
            .order_by(AgentChat.created_at.asc())
            .all()
        )
        enriched = []
        for m in messages:
            d = m.__dict__.copy()
            from_agent = db.query(Agent).filter(Agent.id == m.from_agent_id).first()
            d["from_agent_name"] = from_agent.name if from_agent else "unknown"
            if m.to_agent_id:
                to_agent = db.query(Agent).filter(Agent.id == m.to_agent_id).first()
                d["to_agent_name"] = to_agent.name if to_agent else "unknown"
            enriched.append(ChatMessageResponse.model_validate(d))

        threads.append(ChatThread(
            thread_id=tid,
            messages=enriched,
            latest_at=messages[-1].created_at,
            message_count=len(messages),
        ))

    return threads


@router.post("/{message_id}/read")
async def mark_read(
    message_id: uuid.UUID,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user_or_agent),
):
    """Mark a message addressed to one of your agents as read."""
    msg = db.query(AgentChat).filter(AgentChat.id == message_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.to_agent_id is None or not owns_agent(db, current, msg.to_agent_id):
        raise HTTPException(status_code=403, detail="only the recipient can mark a message read")
    msg.is_read = True
    db.commit()
    return {"status": "ok"}


@router.get("/unread-count")
async def unread_count(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user_or_agent),
):
    """Count unread messages addressed to the principal's agents."""
    mine = principal_agent_ids(db, current)
    if not mine:
        return {"unread": 0}
    count = (
        db.query(AgentChat)
        .filter(
            AgentChat.to_agent_id.in_(mine),
            AgentChat.is_read == False,  # noqa: E712
        )
        .count()
    )
    return {"unread": count}
