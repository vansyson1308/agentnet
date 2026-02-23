from sqlalchemy import Column, String, Integer, Float, Text, DateTime, ForeignKey, Enum, JSON, Numeric, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import enum
import uuid
from .database import Base

# Enum classes
class AgentStatus(str, enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNVERIFIED = "unverified"
    BANNED = "banned"
    SUSPENDED = "suspended"

class WalletOwnerType(str, enum.Enum):
    USER = "user"
    AGENT = "agent"

class TaskStatus(str, enum.Enum):
    INITIATED = "initiated"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    REFUNDED = "refunded"

class TransactionStatus(str, enum.Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class TransactionType(str, enum.Enum):
    PAYMENT = "payment"
    REFERRAL_REWARD = "referral_reward"
    WITHDRAW = "withdraw"
    DEPOSIT = "deposit"
    REFUND = "refund"

class CurrencyType(str, enum.Enum):
    CREDITS = "credits"
    USDC = "usdc"

# Agent model
class Agent(Base):
    __tablename__ = "agents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    name = Column(String, nullable=False)
    description = Column(Text)
    capabilities = Column(JSON, nullable=False, default=[])
    endpoint = Column(String, nullable=False)
    public_key = Column(String, nullable=False)
    status = Column(Enum(AgentStatus), default=AgentStatus.UNVERIFIED)
    verify_score = Column(Integer, default=0)
    timeout_count = Column(Integer, default=0)
    offer_rate_7d = Column(Float, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

# Wallet model
class Wallet(Base):
    __tablename__ = "wallets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_type = Column(Enum(WalletOwnerType), nullable=False)
    owner_id = Column(UUID(as_uuid=True), nullable=False)
    balance_credits = Column(Integer, nullable=False, default=0)
    balance_usdc = Column(Numeric(20, 6), nullable=False, default=0)
    reserved_credits = Column(Integer, nullable=False, default=0)
    reserved_usdc = Column(Numeric(20, 6), nullable=False, default=0)
    spending_cap = Column(Integer, nullable=False, default=1000)
    daily_spent = Column(Integer, nullable=False, default=0)
    daily_reset_at = Column(DateTime(timezone=True), server_default=func.now())
    allowance_parent_id = Column(UUID(as_uuid=True), ForeignKey("wallets.id"))
    auto_approve_threshold = Column(Integer, default=10)
    whitelist = Column(JSON, default=[])
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

# User model (minimal fields needed for the worker)
class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, nullable=False)
    telegram_id = Column(String)
    notification_settings = Column(JSON, default={})

# TaskSession model
class TaskSession(Base):
    __tablename__ = "task_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trace_id = Column(UUID(as_uuid=True), nullable=False)
    span_id = Column(UUID(as_uuid=True), nullable=False)
    parent_span_id = Column(UUID(as_uuid=True))
    caller_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"))
    callee_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"))
    capability = Column(String, nullable=False)
    input_hash = Column(String)
    escrow_amount = Column(Integer, nullable=False)
    currency = Column(Enum(CurrencyType), nullable=False, default=CurrencyType.CREDITS)
    status = Column(Enum(TaskStatus), default=TaskStatus.INITIATED)
    timeout_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True))
    refund_at = Column(DateTime(timezone=True))
    error_message = Column(Text)
    output = Column(JSON)

# Transaction model
class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_wallet = Column(UUID(as_uuid=True), ForeignKey("wallets.id"))
    to_wallet = Column(UUID(as_uuid=True), ForeignKey("wallets.id"))
    amount = Column(Integer, nullable=False)
    currency = Column(Enum(CurrencyType), nullable=False, default=CurrencyType.CREDITS)
    status = Column(Enum(TransactionStatus), default=TransactionStatus.PENDING)
    type = Column(Enum(TransactionType), nullable=False)
    task_session_id = Column(UUID(as_uuid=True), ForeignKey("task_sessions.id"))
    metadata = Column(JSON, default={})
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True))