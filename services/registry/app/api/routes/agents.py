import json
import uuid
from typing import List, Optional

import httpx
import jsonschema
from fastapi import APIRouter, Depends, HTTPException, Query, status
from jsonschema import validate
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ...a2a import agent_to_a2a_card
from ...auth import get_current_agent, get_current_user
from ...database import get_db
from ...models import Agent, AgentStatus, User, Wallet, WalletOwnerType
from ...reputation import compute_agent_reputation
from ...sandbox import SSRFError, SandboxError, SandboxTimeoutError, sandboxed_call
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
    """Update agent info."""
    # Check if the agent exists and belongs to the current user
    if current_agent.id != agent_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to update this agent",
        )

    # Update the agent
    update_data = agent_update.model_dump(exclude_unset=True)

    # Validate capabilities if provided
    if "capabilities" in update_data and agent_update.capabilities is not None:
        for capability in agent_update.capabilities:
            # Check if input_schema and output_schema are valid JSON Schema
            try:
                validate(instance={}, schema=capability.input_schema)
                validate(instance={}, schema=capability.output_schema)
            except jsonschema.ValidationError as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid schema for capability {capability.name}: {str(e)}",
                )

        # Convert Capability objects to dictionaries (outside the loop)
        update_data["capabilities"] = [cap.model_dump() for cap in agent_update.capabilities]

    # Update the agent in the database
    for key, value in update_data.items():
        setattr(current_agent, key, value)

    db.commit()
    db.refresh(current_agent)

    return current_agent


@router.get("/", response_model=List[AgentSchema])
async def search_agents(
    capability: Optional[str] = Query(None, description="Filter by capability name"),
    min_rating: Optional[int] = Query(None, ge=0, le=100, description="Minimum verification score"),
    max_price: Optional[float] = Query(None, ge=0, description="Maximum price for the capability"),
    status: Optional[AgentStatus] = Query(None, description="Filter by agent status"),
    skip: int = Query(0, ge=0, description="Skip records"),
    limit: int = Query(100, ge=1, le=1000, description="Limit records"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Search agents."""
    query = db.query(Agent)

    # Apply filters
    if capability:
        # Use JSONB containment operator to filter by capability
        query = query.filter(Agent.capabilities.contains([{"name": capability}]))

    if min_rating is not None:
        query = query.filter(Agent.verify_score >= min_rating)

    if status:
        query = query.filter(Agent.status == status)

    # Execute query with pagination
    agents = query.offset(skip).limit(limit).all()

    # If max_price is specified, filter agents that have the capability with price <= max_price
    if max_price is not None and capability:
        filtered_agents = []
        for agent in agents:
            for cap in agent.capabilities:
                if cap["name"] == capability and cap.get("price", 0) <= max_price:
                    filtered_agents.append(agent)
                    break
        agents = filtered_agents

    return agents


@router.post("/{agent_id}/verify-capability", response_model=CapabilityVerifyResponse)
async def verify_capability(
    agent_id: uuid.UUID,
    verify_request: CapabilityVerify,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Challenge agent to verify capability."""
    # Get the agent
    db_agent = db.query(Agent).filter(Agent.id == agent_id).first()

    if db_agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # Check if the agent has the requested capability
    capability = None
    for cap in db_agent.capabilities:
        if cap["name"] == verify_request.capability:
            capability = cap
            break

    if not capability:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Agent does not have capability {verify_request.capability}",
        )

    # Send a verification request to the agent's endpoint (via sandbox)
    try:
        response = await sandboxed_call(
            url=f"{db_agent.endpoint}/verify",
            method="POST",
            json_body={
                "capability": verify_request.capability,
                "test_input": verify_request.test_input,
            },
        )

        if response.status_code != 200:
            return CapabilityVerifyResponse(
                verified=False,
                message=f"Agent endpoint returned status code {response.status_code}",
            )

        # Validate the response against the expected schema
        try:
            result = response.json()
            validate(instance=result, schema=verify_request.expected_output_schema)

            # Update agent's verify_score
            db_agent.verify_score = min(100, db_agent.verify_score + 10)

            # If this is the first successful verification, set status to active
            if db_agent.status == AgentStatus.UNVERIFIED:
                db_agent.status = AgentStatus.ACTIVE

            db.commit()

            return CapabilityVerifyResponse(verified=True, message="Capability verified successfully")
        except (json.JSONDecodeError, jsonschema.ValidationError) as e:
            # Decrease agent's verify_score
            db_agent.verify_score = max(0, db_agent.verify_score - 5)
            db.commit()

            return CapabilityVerifyResponse(verified=False, message=f"Invalid response: {str(e)}")
    except SSRFError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Security violation: {str(e)}",
        )
    except SandboxTimeoutError:
        db_agent.verify_score = max(0, db_agent.verify_score - 5)
        db_agent.timeout_count += 1
        db.commit()
        return CapabilityVerifyResponse(verified=False, message="Agent endpoint timed out")
    except SandboxError as e:
        db_agent.verify_score = max(0, db_agent.verify_score - 5)
        db.commit()
        return CapabilityVerifyResponse(verified=False, message=f"Sandbox error: {str(e)}")
    except httpx.RequestError as e:
        # Decrease agent's verify_score
        db_agent.verify_score = max(0, db_agent.verify_score - 5)
        db.commit()

        return CapabilityVerifyResponse(verified=False, message=f"Failed to connect to agent endpoint: {str(e)}")


@router.post("/{agent_id}/report")
async def report_task(
    agent_id: uuid.UUID,
    report: TaskReport,
    db: Session = Depends(get_db),
    current_agent: Agent = Depends(get_current_agent),
):
    """Report task result (update reputation)."""
    # Check if the agent exists
    db_agent = db.query(Agent).filter(Agent.id == agent_id).first()

    if db_agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # Update agent's reputation based on the report
    if report.success:
        # Increase verify_score for successful tasks
        db_agent.verify_score = min(100, db_agent.verify_score + 1)

        # Reset timeout_count if it was non-zero
        if db_agent.timeout_count > 0:
            db_agent.timeout_count = max(0, db_agent.timeout_count - 1)
    else:
        # Decrease verify_score for failed tasks
        db_agent.verify_score = max(0, db_agent.verify_score - 2)

        # If the failure was due to timeout, increase timeout_count
        if report.feedback and "timeout" in report.feedback.lower():
            db_agent.timeout_count += 1

            # If timeout_count is too high, suspend the agent
            if db_agent.timeout_count >= 5:
                db_agent.status = AgentStatus.SUSPENDED

    db.commit()

    return {"message": "Task report processed successfully"}


# ─────────────────────────────────────────────────────────
# Phase 2D: Proxy Registration (import agent by URL)
# ─────────────────────────────────────────────────────────

class ImportAgentRequest(BaseModel):
    """Request to import an external agent via its URL."""
    url: str
    name_override: Optional[str] = None


@router.post("/import", response_model=AgentSchema, status_code=status.HTTP_201_CREATED)
async def import_agent(
    request: ImportAgentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Import an external agent by fetching its A2A Agent Card.

    Paste a URL, and AgentNet will:
    1. Fetch /.well-known/agent-card.json from the URL (via sandbox)
    2. Parse the A2A Agent Card
    3. Create an agent record in the registry

    Requires sandbox (SSRF protection) to be active.
    """
    base_url = request.url.rstrip("/")
    card_url = f"{base_url}/.well-known/agent-card.json"

    # Fetch the A2A card via sandbox
    try:
        response = await sandboxed_call(
            url=card_url,
            method="GET",
        )
    except SSRFError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Security violation: {str(e)}",
        )
    except SandboxError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch agent card: {str(e)}",
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Agent card endpoint returned {response.status_code}",
        )

    try:
        card = response.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Agent card is not valid JSON",
        )

    # Validate minimum required fields
    if not card.get("name") or not card.get("skills"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Agent card missing required fields: 'name' and 'skills'",
        )

    # Convert A2A skills to AgentNet capabilities
    capabilities = []
    for skill in card.get("skills", []):
        capabilities.append({
            "name": skill.get("id", skill.get("name", "unknown")),
            "version": card.get("version", "1.0"),
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "price": 0,  # External agents set their own pricing
        })

    agent_name = request.name_override or card["name"]

    # Check for duplicate
    existing = db.query(Agent).filter(
        Agent.user_id == current_user.id,
        Agent.name == agent_name,
    ).first()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Agent '{agent_name}' already exists. Use name_override.",
        )

    # Determine endpoint from card
    endpoint = base_url
    for iface in card.get("supportedInterfaces", []):
        if iface.get("url"):
            endpoint = iface["url"]
            break

    # Create agent
    db_agent = Agent(
        id=uuid.uuid4(),
        user_id=current_user.id,
        name=agent_name,
        description=card.get("description", ""),
        capabilities=capabilities,
        endpoint=endpoint,
        public_key="imported-via-a2a-card",
        status=AgentStatus.UNVERIFIED,
    )

    db.add(db_agent)
    db.commit()
    db.refresh(db_agent)

    # Create wallet
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

    return db_agent


# ─────────────────────────────────────────────────────────
# Phase 2E: Reputation-Based Routing
# ─────────────────────────────────────────────────────────

@router.get("/discover/{capability_name}")
async def discover_best_agent(
    capability_name: str,
    max_price: Optional[float] = Query(None, ge=0),
    min_reputation_tier: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Discover the best agent for a given capability.

    Automatically routes to the highest-reputation active agent
    that has the requested capability. This is the "auto-route"
    feature — callers don't need to specify a callee.

    Ranking: reputation_tier → success_rate → verify_score → avg_response_time
    """
    # Query active agents with the capability
    query = db.query(Agent).filter(
        Agent.status == AgentStatus.ACTIVE,
        Agent.capabilities.contains([{"name": capability_name}]),
    )

    # Filter by reputation tier if requested
    tier_order = {"diamond": 4, "gold": 3, "silver": 2, "bronze": 1, "unranked": 0}
    if min_reputation_tier and min_reputation_tier in tier_order:
        min_tier_val = tier_order[min_reputation_tier]
        valid_tiers = [t for t, v in tier_order.items() if v >= min_tier_val]
        query = query.filter(Agent.reputation_tier.in_(valid_tiers))

    agents = query.all()

    if not agents:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active agents found with capability '{capability_name}'",
        )

    # Filter by max_price if specified
    if max_price is not None:
        filtered = []
        for agent in agents:
            for cap in agent.capabilities:
                if cap.get("name") == capability_name and cap.get("price", 0) <= max_price:
                    filtered.append(agent)
                    break
        agents = filtered

    if not agents:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No agents found within price range for '{capability_name}'",
        )

    # Rank by: tier (desc) → success_rate (desc) → verify_score (desc) → avg_response_time (asc)
    def rank_key(a):
        return (
            tier_order.get(a.reputation_tier, 0),
            a.success_rate or 0,
            a.verify_score or 0,
            -(a.avg_response_time_ms or 999999),  # Lower is better, so negate
        )

    agents.sort(key=rank_key, reverse=True)

    # Return top 5 recommendations
    results = []
    for agent in agents[:5]:
        # Find the specific capability price
        cap_price = 0
        for cap in agent.capabilities:
            if cap.get("name") == capability_name:
                cap_price = cap.get("price", 0)
                break

        results.append({
            "agent_id": str(agent.id),
            "name": agent.name,
            "description": agent.description,
            "reputation_tier": agent.reputation_tier,
            "success_rate": agent.success_rate,
            "verify_score": agent.verify_score,
            "avg_response_time_ms": agent.avg_response_time_ms,
            "price": cap_price,
            "total_tasks_completed": agent.total_tasks_completed,
        })

    return {
        "capability": capability_name,
        "total_matches": len(agents),
        "recommendations": results,
        "best_match": results[0] if results else None,
    }
