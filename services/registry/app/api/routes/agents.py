from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from typing import List, Optional
import uuid
import json
import jsonschema
from jsonschema import validate
import httpx

from ...database import get_db
from ...models import Agent, User, AgentStatus, Wallet, WalletOwnerType
from ...schemas import (
    AgentCreate, AgentUpdate, Agent as AgentSchema, 
    CapabilityVerify, CapabilityVerifyResponse, TaskReport
)
from ...auth import get_current_user, get_current_agent

router = APIRouter()

@router.post("/", response_model=AgentSchema, status_code=status.HTTP_201_CREATED)
async def create_agent(
    agent: AgentCreate, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
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
                detail=f"Invalid schema for capability {capability.name}: {str(e)}"
            )
    
    # Check if agent with the same name already exists for this user
    db_agent = db.query(Agent).filter(
        Agent.user_id == current_user.id,
        Agent.name == agent.name
    ).first()
    
    if db_agent:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Agent with this name already exists"
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
        status=AgentStatus.UNVERIFIED
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
        daily_spent=0
    )
    
    db.add(db_wallet)
    db.commit()
    
    # Return the agent
    return db_agent

@router.get("/{agent_id}", response_model=AgentSchema)
async def get_agent(
    agent_id: uuid.UUID,
    db: Session = Depends(get_db)
):
    """Get agent details (including reputation)."""
    db_agent = db.query(Agent).filter(Agent.id == agent_id).first()
    
    if db_agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    
    return db_agent

@router.put("/{agent_id}", response_model=AgentSchema)
async def update_agent(
    agent_id: uuid.UUID,
    agent_update: AgentUpdate,
    db: Session = Depends(get_db),
    current_agent: Agent = Depends(get_current_agent)
):
    """Update agent info."""
    # Check if the agent exists and belongs to the current user
    if current_agent.id != agent_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to update this agent"
        )
    
    # Update the agent
    update_data = agent_update.model_dump(exclude_unset=True)
    
    # Validate capabilities if provided
    if "capabilities" in update_data:
        for capability in update_data["capabilities"]:
            # Check if input_schema and output_schema are valid JSON Schema
            try:
                validate(instance={}, schema=capability.input_schema)
                validate(instance={}, schema=capability.output_schema)
            except jsonschema.ValidationError as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid schema for capability {capability.name}: {str(e)}"
                )
            
            # Convert Capability objects to dictionaries
            update_data["capabilities"] = [cap.model_dump() for cap in update_data["capabilities"]]
    
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
    current_user: User = Depends(get_current_user)
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
    current_user: User = Depends(get_current_user)
):
    """Challenge agent to verify capability."""
    # Get the agent
    db_agent = db.query(Agent).filter(Agent.id == agent_id).first()
    
    if db_agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    
    # Check if the agent has the requested capability
    capability = None
    for cap in db_agent.capabilities:
        if cap["name"] == verify_request.capability:
            capability = cap
            break
    
    if not capability:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Agent does not have capability {verify_request.capability}"
        )
    
    # Send a verification request to the agent's endpoint
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{db_agent.endpoint}/verify",
                json={
                    "capability": verify_request.capability,
                    "test_input": verify_request.test_input
                },
                timeout=30.0
            )
        
        if response.status_code != 200:
            return CapabilityVerifyResponse(
                verified=False,
                message=f"Agent endpoint returned status code {response.status_code}"
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
            
            return CapabilityVerifyResponse(
                verified=True,
                message="Capability verified successfully"
            )
        except (json.JSONDecodeError, jsonschema.ValidationError) as e:
            # Decrease agent's verify_score
            db_agent.verify_score = max(0, db_agent.verify_score - 5)
            db.commit()
            
            return CapabilityVerifyResponse(
                verified=False,
                message=f"Invalid response: {str(e)}"
            )
    except httpx.RequestError as e:
        # Decrease agent's verify_score
        db_agent.verify_score = max(0, db_agent.verify_score - 5)
        db.commit()
        
        return CapabilityVerifyResponse(
            verified=False,
            message=f"Failed to connect to agent endpoint: {str(e)}"
        )

@router.post("/{agent_id}/report")
async def report_task(
    agent_id: uuid.UUID,
    report: TaskReport,
    db: Session = Depends(get_db),
    current_agent: Agent = Depends(get_current_agent)
):
    """Report task result (update reputation)."""
    # Check if the agent exists
    db_agent = db.query(Agent).filter(Agent.id == agent_id).first()
    
    if db_agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    
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