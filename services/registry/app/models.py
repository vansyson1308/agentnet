import enum
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .database import Base


def _enum_column(enum_cls, **kwargs):
    """Create an enum column that uses string values (not enum names)."""
    return Column(Enum(enum_cls, native_enum=False, values_callable=lambda x: [e.value for e in x]), **kwargs)


# Enum classes
class KYCStatus(str, enum.Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


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


class SpanStatus(str, enum.Enum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"


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


class ReferralStatus(str, enum.Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    REJECTED = "rejected"


class OfferStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class CurrencyType(str, enum.Enum):
    CREDITS = "credits"
    USDC = "usdc"


# User model
class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, nullable=False)
    phone = Column(String)
    password_hash = Column(String, nullable=False)
    kyc_status = _enum_column(KYCStatus, default="pending")
    telegram_id = Column(String)
    notification_settings = Column(JSON, default={})
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    agents = relationship("Agent", back_populates="user", cascade="all, delete-orphan")


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
    status = _enum_column(AgentStatus, default="unverified")
    verify_score = Column(Integer, default=0)
    timeout_count = Column(Integer, default=0)
    offer_rate_7d = Column(Float, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    user = relationship("User", back_populates="agents")
    caller_tasks = relationship(
        "TaskSession",
        foreign_keys="TaskSession.caller_agent_id",
        back_populates="caller_agent",
    )
    callee_tasks = relationship(
        "TaskSession",
        foreign_keys="TaskSession.callee_agent_id",
        back_populates="callee_agent",
    )
    spans = relationship("Span", back_populates="agent")
    inviter_referrals = relationship(
        "Referral",
        foreign_keys="Referral.inviter_agent_id",
        back_populates="inviter_agent",
    )
    invitee_referrals = relationship(
        "Referral",
        foreign_keys="Referral.invitee_agent_id",
        back_populates="invitee_agent",
    )
    sent_offers = relationship("Offer", foreign_keys="Offer.from_agent_id", back_populates="from_agent")
    received_offers = relationship("Offer", foreign_keys="Offer.to_agent_id", back_populates="to_agent")


# Wallet model
class Wallet(Base):
    __tablename__ = "wallets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_type = _enum_column(WalletOwnerType, nullable=False)
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

    # Relationships
    outgoing_transactions = relationship(
        "Transaction",
        foreign_keys="Transaction.from_wallet",
        back_populates="from_wallet_rel",
    )
    incoming_transactions = relationship(
        "Transaction",
        foreign_keys="Transaction.to_wallet",
        back_populates="to_wallet_rel",
    )


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
    currency = _enum_column(CurrencyType, nullable=False, default="credits")
    status = _enum_column(TaskStatus, default="initiated")
    timeout_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True))
    refund_at = Column(DateTime(timezone=True))
    error_message = Column(Text)
    output = Column(JSON)

    # Relationships
    caller_agent = relationship("Agent", foreign_keys=[caller_agent_id], back_populates="caller_tasks")
    callee_agent = relationship("Agent", foreign_keys=[callee_agent_id], back_populates="callee_tasks")
    transactions = relationship("Transaction", back_populates="task_session")
    offers = relationship("Offer", back_populates="core_task")


# Span model
class Span(Base):
    __tablename__ = "spans"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trace_id = Column(UUID(as_uuid=True), nullable=False)
    span_id = Column(UUID(as_uuid=True), nullable=False)
    parent_span_id = Column(UUID(as_uuid=True))
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"))
    event = Column(String, nullable=False)
    capability = Column(String)
    duration_ms = Column(Integer)
    status = _enum_column(SpanStatus)
    credits_used = Column(Integer)
    extra_data = Column(JSON, default={})
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    agent = relationship("Agent", back_populates="spans")


# Transaction model
class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_wallet = Column(UUID(as_uuid=True), ForeignKey("wallets.id"))
    to_wallet = Column(UUID(as_uuid=True), ForeignKey("wallets.id"))
    amount = Column(Integer, nullable=False)
    currency = _enum_column(CurrencyType, nullable=False, default="credits")
    status = _enum_column(TransactionStatus, default="pending")
    type = _enum_column(TransactionType, nullable=False)
    task_session_id = Column(UUID(as_uuid=True), ForeignKey("task_sessions.id"))
    extra_data = Column(JSON, default={})
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True))

    # Relationships
    from_wallet_rel = relationship("Wallet", foreign_keys=[from_wallet], back_populates="outgoing_transactions")
    to_wallet_rel = relationship("Wallet", foreign_keys=[to_wallet], back_populates="incoming_transactions")
    task_session = relationship("TaskSession", back_populates="transactions")


# Referral model
class Referral(Base):
    __tablename__ = "referrals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    inviter_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"))
    invitee_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"))
    status = _enum_column(ReferralStatus, default="pending")
    reward_amount = Column(Integer)
    device_fingerprint = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True))

    # Relationships
    inviter_agent = relationship("Agent", foreign_keys=[inviter_agent_id], back_populates="inviter_referrals")
    invitee_agent = relationship("Agent", foreign_keys=[invitee_agent_id], back_populates="invitee_referrals")


# Offer model
class Offer(Base):
    __tablename__ = "offers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"))
    to_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"))
    core_task_id = Column(UUID(as_uuid=True), ForeignKey("task_sessions.id"))
    title = Column(String, nullable=False)
    description = Column(Text)
    price = Column(Integer, nullable=False)
    currency = _enum_column(CurrencyType, nullable=False, default="credits")
    expires_at = Column(DateTime(timezone=True), nullable=False)
    status = _enum_column(OfferStatus, default="pending")
    baseline_quality_score = Column(Float)
    blocked = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    from_agent = relationship("Agent", foreign_keys=[from_agent_id], back_populates="sent_offers")
    to_agent = relationship("Agent", foreign_keys=[to_agent_id], back_populates="received_offers")
    core_task = relationship("TaskSession", back_populates="offers")
