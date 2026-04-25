import json
import uuid
from typing import List, Optional

import httpx
import jsonschema
from fastapi import APIRouter, Depends, HTTPException, Query, status
from jsonschema import validate
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...a2a import agent_to_a2a_card
from ...auth import get_current_agent, get_current_user, get_current_user_or_agent
from ...database import get_db
from ...models import Agent, AgentStatus, User, Wallet, WalletOwnerType
from ...reputation import compute_agent_reputation
from ...sandbox import SandboxError, SandboxTimeoutError, SSRFError, sandboxed_call
from ...schemas import Agent as AgentSchema
from ...schemas import (
    AgentCreate,
    AgentReputation,
    AgentUpdate,
    CapabilityVerify,
    CapabilityVerifyResponse,
    TaskReport,
)

router = APIRouter()


@router.post("/", response_model=AgentSchema, status_code=status.HTTP_201_CREATED)
async def create_agent(
    agent: AgentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Register a new agent."""
    # Validate capabilities
    for capability in agent.capabilities:
        # Check if input_schema and output_schema are valid JSON Schema
        try:
            validate(instance={}, schema=capability.input_schema)
            validate(instance={}, schema=capability.output_schema)
        except jsonschema.ValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid schema for capability {capability.name}: {str(e)}",
            )

    # Check if agent with the same name already exists for this user
    db_agent = db.query(Agent).filter(Agent.user_id == current_user.id, Agent.name == agent.name).first()

    if db_agent:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Agent with this name already exists",
        )

    # Create the agent
    db_agent = Agent(
        id=uuid.uuid4(),
        user_id=current_user.id,
        name=agent.name,
        description=agent.description,
        capabilities=[cap.model_dump() for cap in agent.capabilities],
        endpoint=agent.endpoint,
        public_key=agent.public_key,
        status=AgentStatus.UNVERIFIED,
    )

    db.add(db_agent)
    db.commit()
    db.refresh(db_agent)

    # Create a wallet for the agent
    db_wallet = Wallet(
        id=uuid.uuid4(),
        owner_type=WalletOwnerType.AGENT,
        owner_id=db_agent.id,
        balance_credits=0,
        balance_usdc=0,
        reserved_credits=0,
        reserved_usdc=0,
        spending_cap=1000,
        daily_spent=0,
    )

    db.add(db_wallet)
    db.commit()

    # Return the agent
    return db_agent


@router.get("/{agent_id}", response_model=AgentSchema)
async def get_agent(agent_id: uuid.UUID, db: Session = Depends(get_db)):
    """Get agent details (including reputation)."""
    db_agent = db.query(Agent).filter(Agent.id == agent_id).first()

    if db_agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    return db_agent


@router.get("/{agent_id}/capabilities")
async def get_agent_capabilities(agent_id: uuid.UUID, db: Session = Depends(get_db)):
    """Get the capabilities of an agent (public discovery endpoint)."""
    db_agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if db_agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return db_agent.capabilities


@router.get("/{agent_id}/reputation", response_model=AgentReputation)
async def get_agent_reputation(agent_id: uuid.UUID, db: Session = Depends(get_db)):
    """
    Get detailed reputation metrics for an agent.

    Computes real-time metrics from task_sessions and spans data:
    success rate, avg response time, reliability, reputation tier.
    """
    db_agent = db.query(Agent).filter(Agent.id == agent_id).first()

    if db_agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    metrics = compute_agent_reputation(db, agent_id)

    total = metrics["total_tasks_completed"] + metrics["total_tasks_failed"] + metrics["total_tasks_timeout"]

    return AgentReputation(
        agent_id=agent_id,
        agent_name=db_agent.name,
        verify_score=db_agent.verify_score,
        success_rate=metrics["success_rate"],
        avg_response_time_ms=metrics["avg_response_time_ms"],
        total_tasks_completed=metrics["total_tasks_completed"],
        total_tasks_failed=metrics["total_tasks_failed"],
        total_tasks_timeout=metrics["total_tasks_timeout"],
        total_volume_credits=metrics["total_volume_credits"],
        reputation_tier=metrics["reputation_tier"],
        reliability=metrics["reliability"],
        timeout_count=db_agent.timeout_count,
        offer_rate_7d=db_agent.offer_rate_7d,
    )


@router.get("/{agent_id}/a2a-card")
async def get_agent_card(agent_id: uuid.UUID, db: Session = Depends(get_db)):
    """
    Get the A2A Agent Card for a specific agent.

    Returns a standard A2A-compatible Agent Card (JSON) that describes
    the agent's capabilities, endpoint, and authentication requirements.
    Any A2A-compatible system can use this to discover and interact with the agent.
    """
    db_agent = db.query(Agent).filter(Agent.id == agent_id).first()

    if db_agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    return agent_to_a2a_card(db_agent)


@router.put("/{agent_id}", response_model=AgentSchema)
async def update_agent(
    agent_id: uuid.UUID,
    agent_update: AgentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update an agent's details."""
    db_agent = db.query(Agent).filter(Agent.id == agent_id, Agent.user_id == current_user.id).first()

    if db_agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    for field, value in agent_update.dict(exclude_unset=True).items():
        setattr(db_agent, field, value)

    db.commit()
    db.refresh(db_agent)
    return db_agent


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete an agent."""
    db_agent = db.query(Agent).filter(Agent.id == agent_id, Agent.user_id == current_user.id).first()

    if db_agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    db.delete(db_agent)
    db.commit()
    return None


@router.post("/{agent_id}/verify-capabilities", response_model=CapabilityVerifyResponse)
async def verify_agent_capabilities(
    agent_id: uuid.UUID,
    capability_verify: CapabilityVerify,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Verify a specific capability of an agent by sending a test payload."""
    db_agent = db.query(Agent).filter(Agent.id == agent_id).first()

    if db_agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # Find the capability
    target_cap = None
    for cap in db_agent.capabilities:
        if cap.get("name") == capability_verify.capability_name:
            target_cap = cap
            break

    if target_cap is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Capability not found")

    # Prepare the A2A request
    a2a_request = {
        "jsonrpc": "2.0",
        "method": "tasks/send",
        "params": {
            "id": str(uuid.uuid4()),
            "sessionId": str(uuid.uuid4()),
            "input": capability_verify.test_input,
        },
        "id": 1,
    }

    try:
        response = await sandboxed_call(
            url=db_agent.endpoint,
            payload=a2a_request,
            timeout=10,
            max_response_size=1024 * 1024,
        )

        # Parse response
        response_data = response.json()

        # Check if response indicates success/failure
        is_valid = "result" in response_data and "status" in response_data["result"]
        error_message = None
        if not is_valid:
            error_message = response_data.get("error", {}).get("message", "Unknown error")

        return CapabilityVerifyResponse(is_valid=is_valid, error_message=error_message, response_body=response_data)

    except (SandboxTimeoutError, SSRFError, SandboxError) as e:
        return CapabilityVerifyResponse(is_valid=False, error_message=str(e), response_body={})
    except Exception as e:
        return CapabilityVerifyResponse(is_valid=False, error_message=f"Unexpected error: {str(e)}", response_body={})


@router.get("/", response_model=List[AgentSchema])
async def list_agents(
    db: Session = Depends(get_db),
    status: Optional[AgentStatus] = Query(None, alias="status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user_or_agent),
):
    """List agents with optional filtering by status."""
    query = db.query(Agent)

    if status:
        query = query.filter(Agent.status == status)

    # If user is authenticated, show their own agents even if unverified,
    # but only show verified/active agents to others
    if current_user is None:
        query = query.filter(Agent.status == AgentStatus.ACTIVE)
    elif not getattr(current_user, "is_admin", False):
        query = query.filter((Agent.user_id == current_user.id) | (Agent.status == AgentStatus.ACTIVE))

    agents = query.offset(skip).limit(limit).all()
    return agents