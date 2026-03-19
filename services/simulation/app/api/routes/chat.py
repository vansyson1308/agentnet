"""
Post-simulation chat endpoints.

POST /{sim_id}/chat — Chat with a simulated agent
GET  /{sim_id}/chat/history — Get chat history
"""

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ...auth import get_current_user_id
from ...database import get_db
from ...models import SimSession, SimStatus
from ...schemas import ChatMessageResponse, ChatRequest, ChatResponse
from ...services.chat_handler import chat_with_agent, get_chat_history

router = APIRouter()


@router.post("/{sim_id}/chat", response_model=ChatResponse)
async def chat_with_simulated_agent(
    sim_id: uuid.UUID,
    chat_req: ChatRequest,
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    """
    Chat with a simulated agent about their behavior.

    Only available for completed simulations.
    """
    session = db.query(SimSession).filter(SimSession.id == sim_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Simulation not found")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    if SimStatus(session.status) != SimStatus.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail="Chat is only available for completed simulations",
        )

    try:
        result = await chat_with_agent(
            db=db,
            session=session,
            agent_index=chat_req.agent_index,
            message=chat_req.message,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return ChatResponse(
        user_message=ChatMessageResponse(
            id=result["user_message"].id,
            agent_index=result["user_message"].agent_index,
            role=result["user_message"].role,
            content=result["user_message"].content,
            created_at=result["user_message"].created_at,
        ),
        agent_response=ChatMessageResponse(
            id=result["agent_response"].id,
            agent_index=result["agent_response"].agent_index,
            role=result["agent_response"].role,
            content=result["agent_response"].content,
            created_at=result["agent_response"].created_at,
        ),
    )


@router.get("/{sim_id}/chat/history", response_model=List[ChatMessageResponse])
async def get_chat_history_endpoint(
    sim_id: uuid.UUID,
    agent_index: int = Query(..., ge=0),
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    """Get chat history for a specific simulated agent."""
    session = db.query(SimSession).filter(SimSession.id == sim_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Simulation not found")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")

    messages = get_chat_history(db, sim_id, agent_index)
    return [
        ChatMessageResponse(
            id=m.id,
            agent_index=m.agent_index,
            role=m.role,
            content=m.content,
            created_at=m.created_at,
        )
        for m in messages
    ]
