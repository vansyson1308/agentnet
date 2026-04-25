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

    card = agent_to_a2a_card(db_agent)
    return card.model_dump(by_alias=True, exclude_none=True)


@router.put("/{agent_id}", response_model=AgentSchema)
async def update_agent(
    agent_id: uuid.UUID,
    agent_update: AgentUpdate,
    db: Session = Depends(get_db),
    current_agent: Agent = Depends(get_current_agent),
):
    """Update an existing agent."""
    db_agent = db.query(Agent).filter(Agent.id == agent_id).first()

    if db_agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # Verify ownership
    if db_agent.user_id != current_agent.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to update this agent")

    # Update fields if provided
    if agent_update.name is not None:
        db_agent.name = agent_update.name
    if agent_update.description is not None:
        db_agent.description = agent_update.description
    if agent_update.capabilities is not None:
        # Validate new capabilities
        for capability in agent_update.capabilities:
            try:
                validate(instance={}, schema=capability.input_schema)
                validate(instance={}, schema=capability.output_schema)
            except jsonschema.ValidationError as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid schema for capability {capability.name}: {str(e)}",
                )
        db_agent.capabilities = [cap.model_dump() for cap in agent_update.capabilities]
    if agent_update.endpoint is not None:
        db_agent.endpoint = agent_update.endpoint
    if agent_update.public_key is not None:
        db_agent.public_key = agent_update.public_key

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
    db_agent = db.query(Agent).filter(Agent.id == agent_id).first()

    if db_agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # Verify ownership
    if db_agent.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to delete this agent")

    db.delete(db_agent)
    db.commit()
    return None


@router.post("/{agent_id}/verify", response_model=CapabilityVerifyResponse)
async def verify_agent_capability(
    agent_id: uuid.UUID,
    verify_request: CapabilityVerify,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Verify a specific capability of an agent by sending a test payload."""
    db_agent = db.query(Agent).filter(Agent.id == agent_id).first()

    if db_agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # Verify ownership or admin
    if db_agent.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to verify this agent")

    # Find the capability by name
    capability = None
    for cap in db_agent.capabilities:
        if cap["name"] == verify_request.capability_name:
            capability = cap
            break

    if capability is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Capability '{verify_request.capability_name}' not found",
        )

    # Validate test input against input_schema
    try:
        validate(instance=verify_request.test_input, schema=capability["input_schema"])
    except jsonschema.ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Test input does not match input_schema: {str(e)}",
        )

    # Call the agent's endpoint with test input
    try:
        agent_url = db_agent.endpoint.rstrip("/") + f"/{verify_request.capability_name}"
        result = await sandboxed_call(
            agent_url,
            payload=verify_request.test_input,
            timeout=30,
        )
    except (SandboxTimeoutError, SSRFError, SandboxError) as e:
        return CapabilityVerifyResponse(
            success=False,
            error=f"Call failed: {str(e)}",
            output=None,
        )

    # Validate output against output_schema
    try:
        validate(instance=result, schema=capability["output_schema"])
    except jsonschema.ValidationError as e:
        return CapabilityVerifyResponse(
            success=False,
            error=f"Output does not match output_schema: {str(e)}",
            output=result,
        )

    # Update agent status to VERIFIED if not already
    if db_agent.status == AgentStatus.UNVERIFIED:
        db_agent.status = AgentStatus.VERIFIED
        db.commit()

    return CapabilityVerifyResponse(
        success=True,
        error=None,
        output=result,
    )


@router.get("/", response_model=List[AgentSchema])
async def list_agents(
    db: Session = Depends(get_db),
    status: Optional[str] = Query(None, description="Filter by agent status"),
    min_verify_score: Optional[float] = Query(None, ge=0.0, le=1.0),
    max_verify_score: Optional[float] = Query(None, ge=0.0, le=1.0),
    sort_by: Optional[str] = Query(None, regex="^(created_at|verify_score|name)$"),
    sort_order: Optional[str] = Query("desc", regex="^(asc|desc)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
):
    """
    List agents with optional filtering, sorting, and pagination.

    This is a public endpoint that does not require authentication.
    """
    query = db.query(Agent)

    if status:
        try:
            status_enum = AgentStatus[status.upper()]
            query = query.filter(Agent.status == status_enum)
        except KeyError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    if min_verify_score is not None:
        query = query.filter(Agent.verify_score >= min_verify_score)
    if max_verify_score is not None:
        query = query.filter(Agent.verify_score <= max_verify_score)

    # Sorting
    if sort_by:
        sort_column = getattr(Agent, sort_by, None)
        if sort_column is None:
            raise HTTPException(status_code=400, detail=f"Invalid sort field: {sort_by}")
        if sort_order == "desc":
            query = query.order_by(sort_column.desc())
        else:
            query = query.order_by(sort_column.asc())

    agents = query.offset(offset).limit(limit).all()

    return agents