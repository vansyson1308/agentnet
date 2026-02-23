from pydantic import BaseModel, EmailStr, Field, field_validator, UUID4
from typing import List, Optional, Dict, Any, Union
from datetime import datetime
from enum import Enum
import json
import re

# User schemas
class UserBase(BaseModel):
    email: EmailStr
    phone: Optional[str] = None

class UserCreate(UserBase):
    password: str = Field(..., min_length=8)

    @field_validator('password')
    def password_strength(cls, v):
        if not re.search(r'[A-Z]', v):
            raise ValueError('Password must contain at least one uppercase letter')
        if not re.search(r'[a-z]', v):
            raise ValueError('Password must contain at least one lowercase letter')
        if not re.search(r'[0-9]', v):
            raise ValueError('Password must contain at least one digit')
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

    class Config:
        from_attributes = True

# Task schemas
class TaskCreate(BaseModel):
    caller_agent_id: UUID4
    callee_agent_id: UUID4
    capability: str
    input: Dict[str, Any]
    max_budget: int
    currency: str = "credits"
    timeout_seconds: int = 300

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
    input_hash: Optional[str] = None
    escrow_amount: int
    currency: str
    status: str
    timeout_at: datetime
    created_at: datetime
    completed_at: Optional[datetime] = None
    refund_at: Optional[datetime] = None
    error_message: Optional[str] = None
    output: Optional[Dict[str, Any]] = None

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
    escrow_amount: int
    currency: str
    status: str
    timeout_at: datetime
    created_at: datetime
    completed_at: Optional[datetime] = None
    refund_at: Optional[datetime] = None
    error_message: Optional[str] = None
    output: Optional[Dict[str, Any]] = None

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
    user_id: Optional[UUID4] = None
    agent_id: Optional[UUID4] = None

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
    core_task_id: UUID4
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

# Error response schema
class ErrorResponse(BaseModel):
    detail: str
    code: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None