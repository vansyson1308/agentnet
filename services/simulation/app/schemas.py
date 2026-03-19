"""
Pydantic schemas for the simulation service API.
"""

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

# ─── Seed Configuration ─────────────────────────────────────


class AgentFilter(BaseModel):
    """Filter criteria for selecting agents as simulation seed."""

    capabilities: Optional[List[str]] = None
    min_reputation_tier: Optional[str] = None
    status: str = "active"
    limit: int = Field(default=50, ge=1, le=500)


class SeedConfig(BaseModel):
    """Configuration for extracting seed data from AgentNet."""

    source: str = Field(default="social_graph", description="Data source: social_graph or manual")
    agent_filter: AgentFilter = Field(default_factory=AgentFilter)
    include_interactions: bool = True
    include_task_history: bool = True
    time_range_days: int = Field(default=90, ge=1, le=365)


# ─── Simulation Configuration ───────────────────────────────


class InjectedAgent(BaseModel):
    """A synthetic agent to inject into the simulation."""

    name: str
    description: Optional[str] = None
    capabilities: Optional[List[str]] = None
    personality_traits: Optional[Dict[str, Any]] = None
    pricing_strategy: Optional[str] = None


class SimulationParams(BaseModel):
    """Parameters for the OASIS simulation engine."""

    platform: str = Field(default="twitter", description="Simulation platform: twitter or reddit")
    num_steps: int = Field(default=100, ge=10, le=1000)
    scenario: Optional[str] = Field(
        default=None,
        description="Natural language description of the scenario to simulate",
    )
    injected_agents: Optional[List[InjectedAgent]] = None

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, v):
        if v not in ("twitter", "reddit"):
            raise ValueError("Platform must be 'twitter' or 'reddit'")
        return v


class PaymentConfig(BaseModel):
    """Payment configuration for the simulation."""

    max_budget: int = Field(ge=0, description="Maximum credits to spend")
    currency: str = "credits"


# ─── Request Schemas ─────────────────────────────────────────


class SimulationCreate(BaseModel):
    """Request to create and start a simulation."""

    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    seed_config: SeedConfig = Field(default_factory=SeedConfig)
    simulation_config: SimulationParams = Field(default_factory=SimulationParams)
    payment: Optional[PaymentConfig] = None


class SimulationPreview(BaseModel):
    """Request to preview simulation cost without starting."""

    seed_config: SeedConfig = Field(default_factory=SeedConfig)
    simulation_config: SimulationParams = Field(default_factory=SimulationParams)


class ChatRequest(BaseModel):
    """Request to chat with a simulated agent."""

    agent_index: int = Field(ge=0)
    message: str = Field(min_length=1, max_length=5000)


# ─── Response Schemas ────────────────────────────────────────


class SimulationResponse(BaseModel):
    """Response for a simulation session."""

    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    description: Optional[str] = None
    status: str
    platform: str
    num_steps: int
    num_simulated_agents: int
    cost_credits: int
    progress_pct: int
    error_message: Optional[str] = None
    task_session_id: Optional[uuid.UUID] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SimulationPreviewResponse(BaseModel):
    """Response for simulation cost preview."""

    estimated_cost: int
    num_seed_agents: int
    num_interactions: int
    platform: str
    num_steps: int
    warnings: List[str] = []


class SimAgentProfileResponse(BaseModel):
    """Response for a simulated agent profile."""

    id: uuid.UUID
    persona_name: str
    persona_data: Dict[str, Any]
    source_agent_id: Optional[uuid.UUID] = None
    is_injected: bool
    agent_index: int

    class Config:
        from_attributes = True


class SimResultResponse(BaseModel):
    """Response for a simulation result entry."""

    id: uuid.UUID
    step_number: int
    agent_index: int
    action_type: Optional[str] = None
    content: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    created_at: datetime

    class Config:
        from_attributes = True


class SimReportResponse(BaseModel):
    """Response for a simulation report."""

    id: uuid.UUID
    report_type: str
    title: Optional[str] = None
    content: str
    summary: Optional[str] = None
    key_findings: Optional[Dict[str, Any]] = None
    confidence_score: Optional[float] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ChatMessageResponse(BaseModel):
    """Response for a chat message."""

    id: uuid.UUID
    agent_index: int
    role: str
    content: str
    created_at: datetime

    class Config:
        from_attributes = True


class ChatResponse(BaseModel):
    """Response for a chat interaction."""

    user_message: ChatMessageResponse
    agent_response: ChatMessageResponse
