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
from sqlalchemy.dialects.postgresql import INET, JSONB as PG_JSONB
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


class InteractionType(str, enum.Enum):
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    OFFER_ACCEPTED = "offer_accepted"
    OFFER_REJECTED = "offer_rejected"
    REFERRAL = "referral"


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
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan")


# Agent model
class Agent(Base):
    __tablename__ = "agents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    name = Column(String, nullable=False)
    description = Column(Text)
    capabilities = Column(PG_JSONB, nullable=False, default=[])
    endpoint = Column(String, nullable=False)
    public_key = Column(String, nullable=False)
    status = _enum_column(AgentStatus, default="unverified")
    verify_score = Column(Integer, default=0)
    timeout_count = Column(Integer, default=0)
    offer_rate_7d = Column(Float, default=0)
    # Enhanced reputation fields (Phase 2B)
    total_tasks_completed = Column(Integer, default=0)
    total_tasks_failed = Column(Integer, default=0)
    total_tasks_timeout = Column(Integer, default=0)
    success_rate = Column(Float, default=0.0)  # completed / total
    avg_response_time_ms = Column(Integer, default=0)
    total_volume_credits = Column(Integer, default=0)
    reputation_tier = Column(String, default="unranked")  # unranked/bronze/silver/gold/diamond
    reputation_updated_at = Column(DateTime(timezone=True))
    # Heartbeat / online tracking (Phase 1+2)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    current_capability = Column(String, nullable=True)  # which capability agent is currently running
    is_online = Column(Boolean, default=False)  # True if WebSocket connected or heartbeat < 60s
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


# AuditLog model
class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_user_id = Column(UUID(as_uuid=True), nullable=True)
    actor_ip = Column(INET, nullable=True)
    action = Column(String, nullable=False)
    target_id = Column(String, nullable=True)
    payload_summary = Column(Text, nullable=True)
    success = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())