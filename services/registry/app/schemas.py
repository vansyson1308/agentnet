import json
import re
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import UUID4, BaseModel, EmailStr, Field, field_validator
from uuid import UUID as UUIDAny


# User schemas
class UserBase(BaseModel):
    email: EmailStr
    phone: Optional[str] = None


class UserCreate(UserBase):
    password: str = Field(..., min_length=8)

    @field_validator("password")
    def password_strength(cls, v):
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not re.search(r"[0-9]", v):
            raise ValueError("Password must contain at least one digit")
        return v


class UserUpdate(BaseModel):
    phone: Optional[str] = None
    notification_settings: Optional[Dict[str, Any]] = None


class UserInDB(UserBase):
    id: UUID4
    kyc_status: str
    telegram_id: Optional[str] = None
    notification_settings: Dict[str, Any]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class User(UserBase):
    id: UUID4
    kyc_status: str
    telegram_id: Optional[str] = None
    notification_settings: Dict[str, Any]

    class Config:
        from_attributes = True


# Agent schemas
class Capability(BaseModel):
    name: str
    version: str
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    price: float


class AgentBase(BaseModel):
    name: str
    description: Optional[str] = None
    capabilities: List[Capability]
    endpoint: str
    public_key: str


class AgentCreate(AgentBase):
    pass


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    capabilities: Optional[List[Capability]] = None
    endpoint: Optional[str] = None


class AgentInDB(AgentBase):
    id: UUID4
    user_id: UUID4
    status: str
    verify_score: int
    timeout_count: int
    offer_rate_7d: float
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class Agent(AgentBase):
    id: UUID4
    user_id: UUID4
    status: str
    verify_score: int
    timeout_count: int
    offer_rate_7d: float
    # Enhanced reputation fields
    total_tasks_completed: int = 0
    total_tasks_failed: int = 0
    total_tasks_timeout: int = 0
    success_rate: float = 0.0
    avg_response_time_ms: int = 0
    total_volume_credits: int = 0
    reputation_tier: str = "unranked"
    # Heartbeat / online tracking
    is_online: bool = False
    last_seen_at: Optional[datetime] = None
    current_capability: Optional[str] = None

    class Config:
        from_attributes = True


class AgentReputation(BaseModel):
    """Detailed reputation metrics for an agent."""

    agent_id: UUID4
    agent_name: str
    verify_score: int
    success_rate: float
    avg_response_time_ms: int
    total_tasks_completed: int
    total_tasks_failed: int
    total_tasks_timeout: int
    total_volume_credits: int
    reputation_tier: str
    reliability: float  # 1 - (timeouts / total)
    timeout_count: int
    offer_rate_7d: float


# Task schemas
class TaskCreate(BaseModel):
    caller_agent_id: UUID4
    callee_agent_id: UUID4
    capability: str
    input: Dict[str, Any]
    max_budget: int
    currency: str = "credits"
    timeout_seconds: int = 300
    retry_of_id: Optional[UUID4] = None


class TaskUpdate(BaseModel):
    status: Optional[str] = None
    output: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None


class TaskInDB(BaseModel):
    id: UUID4
    trace_id: UUID4
    span_id: UUID4
    parent_span_id: Optional[UUID4] = None
    caller_agent_id: UUID4
    callee_agent_id: UUID4
    capability: str
    input: Optional[Dict[str, Any]] = None
    input_hash: Optional[str] = None
    escrow_amount: int
    currency: str
    status: str
    timeout_at: datetime
    created_at: datetime
    completed_at: Optional[datetime] = None
    refund_at: Optional[datetime] = None
    error_message: Optional[str] = None
    fulfillment_channel: Optional[str] = None
    output: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True


class TaskSummary(BaseModel):
    id: UUID4
    status: str

    class Config:
        from_attributes = True


class Task(BaseModel):
    id: UUID4
    trace_id: UUID4
    span_id: UUID4
    parent_span_id: Optional[UUID4] = None
    caller_agent_id: UUID4
    callee_agent_id: UUID4
    capability: str
    input: Optional[Dict[str, Any]] = None
    escrow_amount: int
    currency: str
    status: str
    timeout_at: datetime
    created_at: datetime
    completed_at: Optional[datetime] = None
    refund_at: Optional[datetime] = None
    error_message: Optional[str] = None
    fulfillment_channel: Optional[str] = None
    output: Optional[Dict[str, Any]] = None
    retry_of_id: Optional[UUID4] = None
    retries: List[TaskSummary] = []

    class Config:
        from_attributes = True


# WebSocket message schemas
class WebSocketMessage(BaseModel):
    jsonrpc: str = "2.0"
    id: str
    trace_id: UUID4
    method: str
    to: Optional[UUID4] = None
    params: Dict[str, Any]


class WebSocketResponse(BaseModel):
    jsonrpc: str = "2.0"
    id: str
    trace_id: UUID4
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    credits_charged: Optional[float] = None


# Span schemas
class SpanCreate(BaseModel):
    trace_id: UUID4
    span_id: UUID4
    parent_span_id: Optional[UUID4] = None
    agent_id: UUID4
    event: str
    capability: Optional[str] = None
    duration_ms: Optional[int] = None
    status: Optional[str] = None
    credits_used: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None


class SpanInDB(SpanCreate):
    id: UUID4
    created_at: datetime

    class Config:
        from_attributes = True


# Capability verification schemas
class CapabilityVerify(BaseModel):
    capability: str
    test_input: Dict[str, Any]
    expected_output_schema: Dict[str, Any]


class CapabilityVerifyResponse(BaseModel):
    verified: bool
    message: Optional[str] = None


# Task report schemas
class TaskReport(BaseModel):
    task_session_id: UUID4
    success: bool
    rating: int
    feedback: Optional[str] = None


# Token schemas
class TokenData(BaseModel):
    user_id: Optional[UUIDAny] = None
    agent_id: Optional[UUIDAny] = None


class UserToken(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class AgentToken(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


# Login schemas
class UserLogin(BaseModel):
    email: EmailStr
    password: str


class AgentLogin(BaseModel):
    agent_id: UUID4
    signature: str
    timestamp: str


# Approval schemas
class ApprovalRequest(BaseModel):
    agent_id: UUID4
    amount: int
    currency: str
    description: str
    callback_url: Optional[str] = None


class ApprovalResponse(BaseModel):
    approval_id: UUID4
    status: str
    message: Optional[str] = None


# Offer schemas
class OfferCreate(BaseModel):
    to_agent_id: UUID4
    core_task_id: Optional[UUID4] = None
    title: str
    description: Optional[str] = None
    price: int
    currency: str = "credits"
    expires_at: datetime


class OfferUpdate(BaseModel):
    status: str


class OfferInDB(OfferCreate):
    id: UUID4
    from_agent_id: UUID4
    baseline_quality_score: Optional[float] = None
    blocked: bool
    created_at: datetime

    class Config:
        from_attributes = True


class Offer(OfferCreate):
    id: UUID4
    from_agent_id: UUID4
    status: str
    baseline_quality_score: Optional[float] = None
    blocked: bool

    class Config:
        from_attributes = True


# Referral schemas
class ReferralCreate(BaseModel):
    invitee_agent_id: UUID4
    device_fingerprint: str


class ReferralUpdate(BaseModel):
    status: str


class ReferralInDB(ReferralCreate):
    id: UUID4
    inviter_agent_id: UUID4
    reward_amount: Optional[int] = None
    created_at: datetime
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class Referral(ReferralCreate):
    id: UUID4
    inviter_agent_id: UUID4
    status: str
    reward_amount: Optional[int] = None

    class Config:
        from_attributes = True


# Negotiation schemas (Phase 2C)
class CounterOfferCreate(BaseModel):
    proposed_price: int = Field(..., gt=0)
    proposed_terms: Optional[str] = None


class NegotiationRoundResponse(BaseModel):
    id: UUID4
    offer_id: UUID4
    round_number: int
    proposed_by_agent_id: UUID4
    proposed_price: int
    proposed_terms: Optional[str] = None
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


class OfferWithNegotiation(BaseModel):
    """Offer with its negotiation history."""

    id: UUID4
    from_agent_id: UUID4
    to_agent_id: UUID4
    title: str
    description: Optional[str] = None
    price: int  # Current/latest price
    currency: str
    status: str
    expires_at: datetime
    created_at: datetime
    negotiation_rounds: List[NegotiationRoundResponse] = []

    class Config:
        from_attributes = True


# Notification schemas
class NotificationResponse(BaseModel):
    id: UUID4
    user_id: UUID4
    type: str
    title: str
    message: str
    url: Optional[str] = None
    is_read: bool
    created_at: datetime

    class Config:
        from_attributes = True


# Error response schema
class ErrorResponse(BaseModel):
    detail: str
    code: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


# ─────────────────────────────────────────────────────────────────────────
# AgentNet Provisioning Protocol (APP) — AB-415 through AB-418
# ─────────────────────────────────────────────────────────────────────────


# — AB-415: Provisioning Catalog —

class ProvisioningProviderBase(BaseModel):
    slug: str
    name: str
    description: Optional[str] = None
    website: Optional[str] = None
    logo_url: Optional[str] = None


class ProvisioningProviderCreate(ProvisioningProviderBase):
    pass


class ProvisioningProviderResponse(ProvisioningProviderBase):
    id: UUID4
    is_active: bool
    created_at: datetime
    services: list[Any] = []  # ProvisioningServiceResponse — updated via model_rebuild()

    class Config:
        from_attributes = True


class ProvisioningServiceBase(BaseModel):
    service_name: str
    description: Optional[str] = None
    category: str
    tier: str = "free"
    pricing_credits: int = 0
    pricing_usdc: float = 0
    regions: list[str] = []
    required_params: list[str] = []
    output_params: dict[str, Any] = {}


class ProvisioningServiceCreate(ProvisioningServiceBase):
    provider_id: UUID4


class ProvisioningServiceResponse(ProvisioningServiceBase):
    id: UUID4
    provider_id: UUID4
    is_active: bool
    created_at: datetime
    provider_slug: str = ""
    provider_name: str = ""

    class Config:
        from_attributes = True


# — AB-416: Scoped API Token —

class ScopedTokenCreate(BaseModel):
    agent_id: UUID4
    resource_type: str
    resource_id: Optional[UUID4] = None
    project_id: Optional[UUID4] = None
    spending_cap: int = 100
    allowed_actions: list[str] = ["read"]
    expires_in: Optional[int] = None  # seconds from now; None = no expiry


class ScopedTokenResponse(BaseModel):
    id: UUID4
    agent_id: UUID4
    resource_type: str
    resource_id: Optional[str] = None
    project_id: Optional[UUID4] = None
    spending_cap: int
    total_spent: int = 0
    allowed_actions: list[str] = []
    expires_at: Optional[datetime] = None
    is_revoked: bool = False
    created_at: datetime
    raw_token: Optional[str] = None  # shown only on creation

    class Config:
        from_attributes = True


# — AB-417: Projects —

class ProjectCreate(BaseModel):
    name: str
    agent_id: UUID4
    description: Optional[str] = None


class ProjectResourceCreate(BaseModel):
    resource_type: str
    resource_ref: str
    provider: str
    scoped_token_id: Optional[UUID4] = None


class ProjectResourceResponse(BaseModel):
    id: UUID4
    project_id: UUID4
    resource_type: str
    resource_ref: Optional[str] = None
    provider: Optional[str] = None
    status: str = "provisioned"
    scoped_token_id: Optional[UUID4] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ProjectResponse(BaseModel):
    id: UUID4
    name: str
    agent_id: Optional[UUID4] = None
    description: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    resources: list[ProjectResourceResponse] = []

    class Config:
        from_attributes = True


class ProjectStateExport(BaseModel):
    """Machine-readable project state — mirrors Stripe state.json."""
    project_id: str
    name: str
    agent_id: Optional[str] = None
    description: Optional[str] = None
    resources: list[dict[str, Any]] = []
    created_at: datetime


# — AB-418: Platform Orchestrator —

class OrchestratorPartnerCreate(BaseModel):
    name: str
    platform_url: Optional[str] = None
    webhook_url: Optional[str] = None


class OrchestratorPartnerResponse(BaseModel):
    id: UUID4
    name: str
    platform_url: Optional[str] = None
    webhook_url: Optional[str] = None
    client_id: str
    is_active: bool
    created_at: datetime
    client_secret: Optional[str] = None  # shown only on creation

    class Config:
        from_attributes = True


class OrchestratorProvisionRequest(BaseModel):
    client_id: str
    client_secret: str
    user_email: str
    project_name: str = "default"


class OrchestratorProvisionResponse(BaseModel):
    user_id: str
    agent_id: str
    wallet_id: str
    project_id: str
    scoped_token: str
    token_id: str
    expires_at: Optional[datetime] = None
    spending_cap: int
    allowed_actions: list[str]


# Resolve forward references
ProvisioningProviderResponse.model_rebuild()

