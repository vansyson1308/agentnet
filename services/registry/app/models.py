import enum
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func, text

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


class AgentMessageType(str, enum.Enum):
    NOTE = "note"
    OFFER = "offer"
    ALERT = "alert"
    SYSTEM = "system"
    PROPOSAL = "proposal"
    REVIEW_RESULT = "review_result"
    REVIEW_REQUEST = "review_request"
    COMPLETED = "completed"


# ── Goal / Improvement / Memory enums (Phase: agent-goals-and-self-improvement) ──


class GoalOwnerType(str, enum.Enum):
    USER = "USER"
    AGENT = "AGENT"
    SOCIETY = "SOCIETY"


class GoalPriority(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class GoalStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProposalSource(str, enum.Enum):
    SELF_REFLECTION = "self_reflection"
    TASK_FAILURE = "task_failure"
    REVIEW_REJECTION = "review_rejection"
    QA_FAILURE = "qa_failure"
    AUDIT = "audit"
    HUMAN_FEEDBACK = "human_feedback"
    RUNTIME_ERROR = "runtime_error"


class ProposalStatus(str, enum.Enum):
    PROPOSED = "PROPOSED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CONVERTED_TO_TASK = "CONVERTED_TO_TASK"
    IMPLEMENTED = "IMPLEMENTED"


class ProposalScope(str, enum.Enum):
    AGENT = "agent"
    PLATFORM = "platform"


class MemoryScope(str, enum.Enum):
    AGENT = "AGENT"
    SOCIETY = "SOCIETY"


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, nullable=False)
    phone = Column(String)
    password_hash = Column(String, nullable=False)
    is_email_verified = Column(Boolean, default=False, server_default=text('false'))
    # Society runtime authority: NULL (ordinary user) | 'operator' | 'event_producer'.
    # Assigned only by operators / the seed CLI — never by an agent intent.
    society_role = Column(String(32))
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
    # Mission + active goal (Phase: agent-goals-and-self-improvement)
    mission = Column(Text)
    current_goal_id = Column(UUID(as_uuid=True), ForeignKey("goals.id", ondelete="SET NULL"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    user = relationship("User", back_populates="agents")
    current_goal = relationship("Goal", foreign_keys=[current_goal_id], post_update=True)
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
    input = Column(JSON)
    input_hash = Column(String)
    escrow_amount = Column(Integer, nullable=False)
    currency = _enum_column(CurrencyType, nullable=False, default="credits")
    status = _enum_column(TaskStatus, default="initiated")
    timeout_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True))
    refund_at = Column(DateTime(timezone=True))
    error_message = Column(Text)
    fulfillment_channel = Column(String)  # 'websocket', 'webhook', 'internal'
    output = Column(JSON)
    retry_of_id = Column(UUID(as_uuid=True), ForeignKey("task_sessions.id"))

    # Relationships
    caller_agent = relationship("Agent", foreign_keys=[caller_agent_id], back_populates="caller_tasks")
    callee_agent = relationship("Agent", foreign_keys=[callee_agent_id], back_populates="callee_tasks")
    transactions = relationship("Transaction", back_populates="task_session")
    offers = relationship("Offer", back_populates="core_task")
    retry_of = relationship("TaskSession", remote_side=[id], backref="retries")


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
    platform_fee = Column(Integer, default=0)
    platform_fee_rate = Column(Numeric(5, 4), default=0.025)
    extra_data = Column(JSON, default={})
    # Idempotency: Idempotency-Key from the inbound HTTP/WS request, scoped
    # globally across the table via a UNIQUE constraint at the DB level.
    # A retry with the same key returns the existing row instead of creating
    # a duplicate transaction (and therefore a duplicate escrow lock).
    idempotency_key = Column(String(64), unique=True, nullable=True, index=True)
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
    negotiation_rounds = relationship(
        "NegotiationRound", back_populates="offer", order_by="NegotiationRound.round_number"
    )


# NegotiationRound model (Phase 2C — multi-round price negotiation)
class NegotiationRound(Base):
    __tablename__ = "negotiation_rounds"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    offer_id = Column(UUID(as_uuid=True), ForeignKey("offers.id"), nullable=False)
    round_number = Column(Integer, nullable=False)
    proposed_by_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    proposed_price = Column(Integer, nullable=False)
    proposed_terms = Column(Text)
    status = _enum_column(OfferStatus, default="pending")  # pending/accepted/rejected
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    offer = relationship("Offer", back_populates="negotiation_rounds")
    proposed_by = relationship("Agent", foreign_keys=[proposed_by_agent_id])


# AgentInteraction model (Phase 3A — Social Graph)
class AgentInteraction(Base):
    __tablename__ = "agent_interactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    to_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    interaction_type = _enum_column(InteractionType, nullable=False)
    count = Column(Integer, nullable=False, default=1)
    total_volume = Column(Integer, nullable=False, default=0)
    last_interaction_at = Column(DateTime(timezone=True), server_default=func.now())
    first_interaction_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    from_agent = relationship("Agent", foreign_keys=[from_agent_id])
    to_agent = relationship("Agent", foreign_keys=[to_agent_id])


# AgentReputationHistory model — daily snapshots of agent reputation metrics
class AgentReputationHistory(Base):
    __tablename__ = "agent_reputation_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    snapshot_date = Column(Date, nullable=False)
    reputation_tier = Column(String, nullable=False)
    success_rate = Column(Float, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationship
    agent = relationship("Agent")


# Notification model
class Notification(Base):
    __tablename__ = "notifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    type = Column(String, nullable=False)
    title = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    url = Column(String)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="notifications")


# AuditLog model -- security event tracking (Phase S1)
class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_user_id = Column(UUID(as_uuid=True), nullable=True)
    actor_ip = Column(String, nullable=True)
    action = Column(String, nullable=False)
    target_id = Column(String, nullable=True)
    payload_summary = Column(Text, nullable=True)
    success = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# Story model
class Story(Base):
    __tablename__ = "stories"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content = Column(Text, nullable=False)
    mood = Column(String, nullable=False, default="neutral")
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True)
    is_published = Column(Boolean, default=True)
    displayed_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationship
    agent = relationship("Agent")


# EmailVerificationToken model
class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    token = Column(String, nullable=False, unique=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationship
    user = relationship("User")


# AgentChat model
class AgentChat(Base):
    __tablename__ = "agent_chat"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    to_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True)
    message_type = _enum_column(AgentMessageType, nullable=False)
    title = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    msg_metadata = Column(JSON, default={})
    thread_id = Column(UUID(as_uuid=True), nullable=False)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    from_agent = relationship("Agent", foreign_keys=[from_agent_id])
    to_agent = relationship("Agent", foreign_keys=[to_agent_id])


# AgentReputationHistory removed — defined above in file


# ─────────────────────────────────────────────────────────────────────────
# Phase: agent-goals-and-self-improvement
# ─────────────────────────────────────────────────────────────────────────


class Goal(Base):
    """An agent's mission target. Agents may own 0..N active goals plus one
    primary mission string on the Agent row itself. Goals form a parent/child
    tree (epic → milestone → goal). Tasks may declare which goal they advance
    via TaskSession.goal_id (added in a later migration if needed).
    """

    __tablename__ = "goals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String, nullable=False)
    description = Column(Text)
    owner_type = _enum_column(GoalOwnerType, nullable=False)
    owner_id = Column(UUID(as_uuid=True), nullable=False)
    priority = _enum_column(GoalPriority, nullable=False, default="medium")
    status = _enum_column(GoalStatus, nullable=False, default="active")
    success_criteria = Column(PG_JSONB, nullable=False, default=list)
    parent_goal_id = Column(UUID(as_uuid=True), ForeignKey("goals.id", ondelete="SET NULL"))
    target_date = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    parent = relationship("Goal", remote_side=[id], backref="children")


class ImprovementProposal(Base):
    """Structured improvement proposal — the spine of the self-improvement
    loop. Either auto-generated by the worker reflection loop from a failed
    task, or manually created by an agent/user. Lifecycle:
    PROPOSED → UNDER_REVIEW → APPROVED/REJECTED → (if APPROVED)
    CONVERTED_TO_TASK → IMPLEMENTED.
    """

    __tablename__ = "improvement_proposals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    proposed_by_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"))
    proposed_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    source = _enum_column(ProposalSource, nullable=False)
    title = Column(String, nullable=False)
    problem = Column(Text)
    root_cause = Column(Text)
    proposed_change = Column(Text)
    expected_benefit = Column(Text)
    risk = Column(Text)
    status = _enum_column(ProposalStatus, nullable=False, default="PROPOSED")
    target_scope = _enum_column(ProposalScope, nullable=False, default="agent")
    importance = Column(Integer, nullable=False, default=50)
    source_task_id = Column(UUID(as_uuid=True), ForeignKey("task_sessions.id", ondelete="SET NULL"))
    converted_task_id = Column(UUID(as_uuid=True), ForeignKey("task_sessions.id", ondelete="SET NULL"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    proposed_by_agent = relationship("Agent", foreign_keys=[proposed_by_agent_id])
    proposed_by_user = relationship("User", foreign_keys=[proposed_by_user_id])
    source_task = relationship("TaskSession", foreign_keys=[source_task_id])
    converted_task = relationship("TaskSession", foreign_keys=[converted_task_id])


class MemoryItem(Base):
    """Society- or agent-scope durable lesson. Persisted lessons keep the
    society from repeating mistakes. Agent-scope rows MUST have an
    agent_id; society-scope rows MUST NOT (enforced by DB CHECK).
    """

    __tablename__ = "memory_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"))
    scope = _enum_column(MemoryScope, nullable=False)
    title = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    tags = Column(PG_JSONB, nullable=False, default=list)
    source_task_id = Column(UUID(as_uuid=True), ForeignKey("task_sessions.id", ondelete="SET NULL"))
    importance = Column(Integer, nullable=False, default=50)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    agent = relationship("Agent", foreign_keys=[agent_id])
    source_task = relationship("TaskSession", foreign_keys=[source_task_id])


# ─────────────────────────────────────────────────────────────────────────
# AgentNet Provisioning Protocol (APP) — AB-415 through AB-418
# ─────────────────────────────────────────────────────────────────────────


class ProvisioningProvider(Base):
    """A service provider registered in the provisioning catalog."""
    __tablename__ = "provisioning_providers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text)
    website = Column(String)
    logo_url = Column(String)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    services = relationship("ProvisioningService", back_populates="provider", cascade="all, delete-orphan")


class ProvisioningService(Base):
    """A provisionable service in the catalog."""
    __tablename__ = "provisioning_services"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_id = Column(UUID(as_uuid=True), ForeignKey("provisioning_providers.id", ondelete="CASCADE"), nullable=False)
    service_name = Column(String, nullable=False)
    description = Column(Text)
    category = Column(String, nullable=False, index=True)  # domain, hosting, storage, db, ai, security
    tier = Column(String, nullable=False, default="free")  # free, starter, pro, enterprise
    pricing_credits = Column(Integer, default=0)  # one-time or monthly in AgentNet credits
    pricing_usdc = Column(Numeric(12, 6), default=0)
    regions = Column(PG_JSONB, default=[])  # ["us-east", "eu-west", "ap-southeast"]
    required_params = Column(PG_JSONB, default=[])  # ["domain_name", "zone_id"]
    output_params = Column(PG_JSONB, default={})  # {"api_token": "...", "nameservers": [...]}
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    provider = relationship("ProvisioningProvider", back_populates="services")


class ScopedToken(Base):
    """Scoped API token — per-resource, per-agent credentials with limits.

    Mirrors Stripe Shared Payment Token + Cloudflare scoped token.
    """
    __tablename__ = "scoped_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_hash = Column(String, nullable=False, index=True)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    resource_type = Column(String, nullable=False)  # domain, bucket, project, account
    resource_id = Column(String)  # external resource ref
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"))
    spending_cap = Column(Integer, nullable=False, default=100)
    total_spent = Column(Integer, nullable=False, default=0)
    allowed_actions = Column(PG_JSONB, default=[])  # ["read", "write", "deploy", "delete"]
    expires_at = Column(DateTime(timezone=True))
    is_revoked = Column(Boolean, default=False)
    revoked_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    agent = relationship("Agent", foreign_keys=[agent_id])
    project = relationship("Project", foreign_keys=[project_id], back_populates="scoped_tokens")


class Project(Base):
    """Persistent resource grouping — mirrors Stripe Projects' state.json."""
    __tablename__ = "projects"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"))
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    agent = relationship("Agent", foreign_keys=[agent_id])
    resources = relationship("ProjectResource", back_populates="project", cascade="all, delete-orphan")
    scoped_tokens = relationship("ScopedToken", back_populates="project")


class ProjectResource(Base):
    """A resource within a project."""
    __tablename__ = "project_resources"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    resource_type = Column(String, nullable=False)  # domain, bucket, database, worker, api_key
    resource_ref = Column(String)  # external identifier
    provider = Column(String)  # cloudflare, vultr, github, huggingface
    status = Column(String, default="provisioned")  # provisioned, active, error, destroyed
    scoped_token_id = Column(UUID(as_uuid=True), ForeignKey("scoped_tokens.id", ondelete="SET NULL"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    project = relationship("Project", back_populates="resources")


class OrchestratorPartner(Base):
    """A third-party platform registered as an orchestrator.

    These platforms can provision AgentNet accounts on behalf of their users.
    """
    __tablename__ = "orchestrator_partners"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    platform_url = Column(String)
    webhook_url = Column(String)  # events: resource.created, resource.deleted, token.expired
    client_id = Column(String, unique=True, nullable=False)
    client_secret_hash = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# ─────────────────────────────────────────────────────────────────────────
# Autonomous Society Runtime v1 — durable events, runs, intents, grants,
# code candidates. DDL: app/society/schema_sql.py (+ migration 0007).
# ─────────────────────────────────────────────────────────────────────────


class SocietyEventStatus(str, enum.Enum):
    PENDING = "pending"          # inserted, not yet routed to agents
    DISPATCHED = "dispatched"    # runs created for every selected agent
    PROCESSED = "processed"      # every run reached a terminal state
    IGNORED = "ignored"          # no subscriber / suppressed by loop guard
    EXPIRED = "expired"          # TTL elapsed before dispatch


class AgentRunStatus(str, enum.Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"            # retryable failure, will be re-queued until max_attempts
    DEAD = "dead"                # exhausted retries / unrecoverable
    SKIPPED = "skipped"          # suppressed (cooldown, budget, circuit breaker)


class IntentRiskClass(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PolicyDecision(str, enum.Enum):
    PENDING = "pending"
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL_REQUIRED = "approval_required"
    INVALID = "invalid"


class IntentExecutionStatus(str, enum.Enum):
    PENDING = "pending"
    EXECUTED = "executed"
    FAILED = "failed"
    DENIED = "denied"
    SKIPPED = "skipped"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"        # human approved; waiting for a worker to resume
    REJECTED = "rejected"        # human rejected; terminal, never executes


class SocietyUserRole(str, enum.Enum):
    OPERATOR = "operator"                # full operator surfaces + approvals + ingress
    EVENT_PRODUCER = "event_producer"    # may inject allow-listed world events only


class ApprovalDecision(str, enum.Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class CodeCandidateStatus(str, enum.Enum):
    REQUESTED = "requested"
    BUILDING = "building"
    BUILT = "built"
    QA_RUNNING = "qa_running"
    QA_PASSED = "qa_passed"
    QA_FAILED = "qa_failed"
    SECURITY_REVIEW = "security_review"
    READY = "ready"              # QA (and security, if required) passed — human may merge
    REJECTED = "rejected"
    FAILED = "failed"
    ABANDONED = "abandoned"


class SocietyEvent(Base):
    """Append-oriented durable event. Redis is only used to *wake* workers;
    this row is the record. ``causation_id`` links to the event whose
    processing produced this one; ``correlation_id`` is shared by the whole
    chain so a story can be reconstructed with one query."""

    __tablename__ = "society_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type = Column(String(128), nullable=False, index=True)
    actor_type = Column(String(32), nullable=False, default="system")
    actor_id = Column(UUID(as_uuid=True))
    subject_type = Column(String(64))
    subject_id = Column(UUID(as_uuid=True))
    payload = Column(PG_JSONB, nullable=False, default=dict)
    correlation_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    causation_id = Column(UUID(as_uuid=True), ForeignKey("society_events.id", ondelete="SET NULL"))
    causation_depth = Column(Integer, nullable=False, default=0)
    idempotency_key = Column(String(160), unique=True)
    status = _enum_column(SocietyEventStatus, nullable=False, default="pending")
    dispatch_note = Column(Text)
    trace_id = Column(UUID(as_uuid=True))
    source_run_id = Column(UUID(as_uuid=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    dispatched_at = Column(DateTime(timezone=True))
    processed_at = Column(DateTime(timezone=True))

    cause = relationship("SocietyEvent", remote_side=[id], backref="effects")
    runs = relationship("AgentRun", back_populates="event")


class AgentRun(Base):
    """One wake-up / cognition cycle of one agent for one event.

    The lease (``worker_id`` + ``lease_expires_at``) makes crash recovery
    safe: an expired lease is re-claimable. No hidden chain-of-thought is
    stored — only ``decision_summary`` and a bounded ``context_summary``.
    """

    __tablename__ = "agent_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    event_id = Column(UUID(as_uuid=True), ForeignKey("society_events.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(64))
    status = _enum_column(AgentRunStatus, nullable=False, default="queued")
    worker_id = Column(String(128))
    lease_expires_at = Column(DateTime(timezone=True))
    attempt = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    not_before = Column(DateTime(timezone=True))
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    model_provider = Column(String(64))
    model_name = Column(String(128))
    prompt_version = Column(String(32))
    context_digest = Column(String(64))
    context_summary = Column(PG_JSONB, nullable=False, default=dict)
    decision_summary = Column(Text)
    intents_count = Column(Integer, nullable=False, default=0)
    tokens_in = Column(Integer)
    tokens_out = Column(Integer)
    cost_usd = Column(Numeric(12, 6), nullable=False, default=0)
    # Model-request accounting (distinct from run attempts): how many HTTP
    # requests the cognition adapter made for this run, how many were retries,
    # and how many timed out.
    model_requests = Column(Integer, nullable=False, default=0)
    model_retries = Column(Integer, nullable=False, default=0)
    model_timeouts = Column(Integer, nullable=False, default=0)
    error = Column(Text)
    sleep_until = Column(DateTime(timezone=True))
    correlation_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    trace_id = Column(UUID(as_uuid=True))
    span_id = Column(UUID(as_uuid=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    agent = relationship("Agent", foreign_keys=[agent_id])
    event = relationship("SocietyEvent", back_populates="runs")
    intents = relationship("AgentIntent", back_populates="run", order_by="AgentIntent.seq")


class AgentIntent(Base):
    """A typed, validated action proposed by the model and adjudicated by
    the policy engine. ``idempotency_key`` is UNIQUE so re-execution after a
    crash cannot double-apply a side effect."""

    __tablename__ = "agent_intents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    seq = Column(Integer, nullable=False)
    intent_type = Column(String(64), nullable=False)
    payload = Column(PG_JSONB, nullable=False, default=dict)
    idempotency_key = Column(String(160), nullable=False, unique=True)
    risk_class = _enum_column(IntentRiskClass, nullable=False, default="low")
    policy_decision = _enum_column(PolicyDecision, nullable=False, default="pending")
    policy_reason = Column(Text)
    execution_status = _enum_column(IntentExecutionStatus, nullable=False, default="pending")
    result = Column(PG_JSONB, nullable=False, default=dict)
    error = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    executed_at = Column(DateTime(timezone=True))
    # Resume lease: an APPROVED intent is claimed by exactly one worker.
    resume_worker_id = Column(String(128))
    resume_lease_expires_at = Column(DateTime(timezone=True))
    resume_attempt = Column(Integer, nullable=False, default=0)

    run = relationship("AgentRun", back_populates="intents")
    agent = relationship("Agent", foreign_keys=[agent_id])
    approval = relationship("IntentApproval", back_populates="intent", uselist=False)


class IntentApproval(Base):
    """Durable human decision on an ``awaiting_approval`` intent. Exactly one
    row per intent (UNIQUE) — approve/reject races are resolved by the row
    lock on the intent, and the first decision wins."""

    __tablename__ = "intent_approvals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    intent_id = Column(UUID(as_uuid=True), ForeignKey("agent_intents.id", ondelete="CASCADE"), nullable=False, unique=True)
    run_id = Column(UUID(as_uuid=True), nullable=False)
    agent_id = Column(UUID(as_uuid=True), nullable=False)
    decided_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    decision = _enum_column(ApprovalDecision, nullable=False)
    reason = Column(Text)
    original_policy_reason = Column(Text)
    decided_at = Column(DateTime(timezone=True), server_default=func.now())
    resumed_at = Column(DateTime(timezone=True))
    executed_at = Column(DateTime(timezone=True))
    final_state = Column(String(32))
    resume_error = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    intent = relationship("AgentIntent", back_populates="approval")
    decided_by = relationship("User", foreign_keys=[decided_by_user_id])


class AgentCapabilityGrant(Base):
    """The ONLY source of an agent's runtime permissions. Written by the
    seed/admin path, never by an intent (policy.py has no code path that
    mutates this table; tests/society/test_policy.py asserts it)."""

    __tablename__ = "agent_capability_grants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, unique=True)
    role = Column(String(64), nullable=False)
    allowed_intents = Column(PG_JSONB, nullable=False, default=list)
    approval_required_intents = Column(PG_JSONB, nullable=False, default=list)
    resource_scopes = Column(PG_JSONB, nullable=False, default=dict)
    risk_ceiling = _enum_column(IntentRiskClass, nullable=False, default="low")
    max_runs_per_hour = Column(Integer, nullable=False, default=20)
    max_intents_per_run = Column(Integer, nullable=False, default=5)
    daily_model_budget_usd = Column(Numeric(12, 6), nullable=False, default=1.0)
    max_task_escrow_credits = Column(Integer, nullable=False, default=0)
    wake_cooldown_seconds = Column(Integer, nullable=False, default=30)
    enabled = Column(Boolean, nullable=False, default=True)
    paused_until = Column(DateTime(timezone=True))
    consecutive_failures = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    agent = relationship("Agent", foreign_keys=[agent_id])


class CodeCandidate(Base):
    """Durable record of one autonomous engineering attempt: an isolated
    worktree/branch produced by the Builder, evaluated by QA (and Security
    when the diff touches risky surfaces). Never merged by the runtime."""

    __tablename__ = "code_candidates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    proposal_id = Column(UUID(as_uuid=True), ForeignKey("improvement_proposals.id", ondelete="SET NULL"))
    task_id = Column(UUID(as_uuid=True), ForeignKey("task_sessions.id", ondelete="SET NULL"))
    goal_id = Column(UUID(as_uuid=True), ForeignKey("goals.id", ondelete="SET NULL"))
    correlation_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    requested_by_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"))
    builder_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"))
    builder_run_id = Column(UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"))
    qa_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"))
    qa_run_id = Column(UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"))
    security_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"))
    security_run_id = Column(UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"))
    title = Column(String(255), nullable=False)
    spec = Column(PG_JSONB, nullable=False, default=dict)
    branch_name = Column(String(255))
    workspace_path = Column(String(512))
    base_sha = Column(String(64))
    head_sha = Column(String(64))
    diff_stat = Column(Text)
    patch_summary = Column(Text)
    changed_files = Column(PG_JSONB, nullable=False, default=list)
    status = _enum_column(CodeCandidateStatus, nullable=False, default="requested")
    qa_report = Column(PG_JSONB, nullable=False, default=dict)
    security_report = Column(PG_JSONB, nullable=False, default=dict)
    requires_security_review = Column(Boolean, nullable=False, default=False)
    error = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    proposal = relationship("ImprovementProposal", foreign_keys=[proposal_id])
    task = relationship("TaskSession", foreign_keys=[task_id])
    goal = relationship("Goal", foreign_keys=[goal_id])
    builder_run = relationship("AgentRun", foreign_keys=[builder_run_id])
    qa_run = relationship("AgentRun", foreign_keys=[qa_run_id])
    security_run = relationship("AgentRun", foreign_keys=[security_run_id])
