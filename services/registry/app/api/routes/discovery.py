"""A2A Agent Card endpoint per RFC 8615 — mỗi agent có card riêng.
Endpoint: GET /v1/agents/{agent_id}/agent-card.json
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from ...database import get_db
from ...models import Agent
from ...a2a import agent_to_a2a_card

router = APIRouter()


@router.get("/{agent_id}/agent-card.json")
async def get_agent_card(agent_id: str, request: Request, db: Session = Depends(get_db)):
    """Serve the A2A Agent Card for a specific registered agent.

    Each agent gets its own /.well-known/agent-card.json equivalent
    at /api/v1/agents/{agent_id}/agent-card.json so anyone can discover
    an individual agent's capabilities, endpoint, and security scheme.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    base_url = str(request.base_url).rstrip("/")
    card = agent_to_a2a_card(agent, base_url)
    return card.model_dump(by_alias=True, exclude_none=True)
