from pydantic import BaseModel, Field, field_validator, UUID4
from typing import List, Optional, Dict, Any, Union
from datetime import datetime
from enum import Enum

# Wallet schemas
class WalletBalance(BaseModel):
    balance_credits: int
    balance_usdc: float
    reserved_credits: int
    reserved_usdc: float
    spending_cap: int
    daily_spent: int

class WalletUpdate(BaseModel):
    spending_cap: Optional[int] = None
    auto_approve_threshold: Optional[int] = None
    whitelist: Optional[List[str]] = None

# Transaction schemas
class TransactionCreate(BaseModel):
    from_wallet: UUID4
    to_wallet: UUID4
    amount: int
    currency: str = "credits"
    type: str
    metadata: Optional[Dict[str, Any]] = None

class TransactionUpdate(BaseModel):
    status: str

class TransactionInDB(BaseModel):
    id: UUID4
    from_wallet: UUID4
    to_wallet: UUID4
    amount: int
    currency: str
    status: str
    type: str
    task_session_id: Optional[UUID4] = None
    metadata: Dict[str, Any]
    created_at: datetime
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class Transaction(BaseModel):
    id: UUID4
    from_wallet: UUID4
    to_wallet: UUID4
    amount: int
    currency: str
    status: str
    type: str
    task_session_id: Optional[UUID4] = None
    metadata: Dict[str, Any]
    created_at: datetime
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True

# Approval schemas
class ApprovalRequestCreate(BaseModel):
    agent_id: UUID4
    amount: int
    currency: str = "credits"
    description: str
    callback_url: Optional[str] = None
    task_session_id: Optional[UUID4] = None  # Link to task for escrow approval
    expires_in_hours: Optional[int] = 24  # Approval timeout

class ApprovalRequestUpdate(BaseModel):
    approved: bool

class ApprovalRequestInDB(BaseModel):
    id: UUID4
    agent_id: UUID4
    user_id: UUID4
    amount: int
    currency: str
    description: str
    callback_url: Optional[str] = None
    status: str
    created_at: datetime
    updated_at: datetime
    responded_at: Optional[datetime] = None
    # New fields for task escrow approvals
    task_session_id: Optional[UUID4] = None
    expires_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    denied_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class ApprovalRequest(BaseModel):
    id: UUID4
    agent_id: UUID4
    user_id: UUID4
    amount: int
    currency: str
    description: str
    callback_url: Optional[str] = None
    status: str
    created_at: datetime

    class Config:
        from_attributes = True

class ApprovalResponse(BaseModel):
    approval_id: UUID4
    status: str
    message: Optional[str] = None

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

# Error response schema
class ErrorResponse(BaseModel):
    detail: str
    code: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None