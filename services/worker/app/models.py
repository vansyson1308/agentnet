"""Worker service ORM.

These models are a *faithful subset* of the shared PostgreSQL schema that
``services/registry/init-db/*.sql`` creates (see
docs/DATABASE_SCHEMA_CONTRACT.md):

* every column declared here has the same name / type / nullability as the
  database column;
* a column the worker never touches may be omitted ONLY if it is nullable
  or has a DB default (otherwise inserts from this service would fail) —
  ``tests/test_db_parity.py`` enforces both rules;
* enums are bound as strings (``native_enum=False``). The DB enum types
  (``agent_status``, ``task_status``, ...) are created by init-db and are
  the truth; this module must never emit ``CREATE TYPE``.
"""

import enum
import uuid

from sqlalchemy import (
    JSON,
    BigInteger,
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
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from .database import Base


def _enum_column(enum_cls, **kwargs):
    """Create an enum column that uses string values (not enum names).

    Same helper as registry/payment: ``native_enum=False`` so the ORM never
    tries to create (or reference) a Postgres enum type of its own — the
    auto-derived names (``agentstatus``, ``taskstatus``...) do not exist in
    the DB, whose types are ``agent_status``, ``task_status``, ...
    """
    return Column(Enum(enum_cls, native_enum=False, values_callable=lambda x: [e.value for e in x]), **kwargs)


# Core tables use naive TIMESTAMP; heartbeat / improvement columns use TIMESTAMPTZ.
NaiveTimestamp = DateTime(timezone=False)
TzTimestamp = DateTime(timezone=True)


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
    status = _enum_column(AgentStatus, default=AgentStatus.UNVERIFIED)
    verify_score = Column(Integer, default=0)
    timeout_count = Column(Integer, default=0)
    offer_rate_7d = Column(Float, default=0)
    # Enhanced reputation fields
    total_tasks_completed = Column(Integer, default=0)
    total_tasks_failed = Column(Integer, default=0)
    total_tasks_timeout = Column(Integer, default=0)
    success_rate = Column(Float, default=0.0)
    avg_response_time_ms = Column(Integer, default=0)
    total_volume_credits = Column(BigInteger, default=0)
    reputation_tier = Column(String, default="unranked")
    reputation_updated_at = Column(NaiveTimestamp)
    # Heartbeat / presence (init-db/08-heartbeat.sql)
    is_online = Column(Boolean, default=False, server_default=text("false"))
    last_seen_at = Column(TzTimestamp)
    current_capability = Column(String)
    created_at = Column(NaiveTimestamp, server_default=func.now())
    updated_at = Column(NaiveTimestamp, server_default=func.now(), onupdate=func.now())


# Wallet model
class Wallet(Base):
    __tablename__ = "wallets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_type = _enum_column(WalletOwnerType, nullable=False)
    owner_id = Column(UUID(as_uuid=True), nullable=False)
    # Money columns are BIGINT in the DB; balances are mutated ONLY by the
    # DB triggers (never by application code).
    balance_credits = Column(BigInteger, nullable=False, default=0)
    balance_usdc = Column(Numeric(20, 6), nullable=False, default=0)
    reserved_credits = Column(BigInteger, nullable=False, default=0)
    reserved_usdc = Column(Numeric(20, 6), nullable=False, default=0)
    spending_cap = Column(BigInteger, nullable=False, default=1000)
    daily_spent = Column(BigInteger, nullable=False, default=0)
    daily_reset_at = Column(NaiveTimestamp, server_default=func.now())
    allowance_parent_id = Column(UUID(as_uuid=True), ForeignKey("wallets.id"))
    auto_approve_threshold = Column(BigInteger, default=10)
    whitelist = Column(JSON, default=[])
    created_at = Column(NaiveTimestamp, server_default=func.now())
    updated_at = Column(NaiveTimestamp, server_default=func.now(), onupdate=func.now())


# User model — full mirror of the users table (password_hash is NOT NULL
# without a DB default, so a subset without it could never insert).
class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, nullable=False)
    phone = Column(String)
    password_hash = Column(String, nullable=False)
    is_email_verified = Column(Boolean, default=False, server_default=text("false"))
    society_role = Column(String(32))
    kyc_status = _enum_column(KYCStatus, default="pending")
    telegram_id = Column(String)
    notification_settings = Column(JSON, default={})
    created_at = Column(NaiveTimestamp, server_default=func.now())
    updated_at = Column(NaiveTimestamp, server_default=func.now(), onupdate=func.now())


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
    escrow_amount = Column(BigInteger, nullable=False)
    currency = _enum_column(CurrencyType, nullable=False, default=CurrencyType.CREDITS)
    status = _enum_column(TaskStatus, default=TaskStatus.INITIATED)
    timeout_at = Column(NaiveTimestamp, nullable=False)
    created_at = Column(NaiveTimestamp, server_default=func.now())
    completed_at = Column(NaiveTimestamp)
    refund_at = Column(NaiveTimestamp)
    error_message = Column(Text)
    fulfillment_channel = Column(String)
    output = Column(JSON)
    retry_of_id = Column(UUID(as_uuid=True), ForeignKey("task_sessions.id"))


# Transaction model
class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_wallet = Column(UUID(as_uuid=True), ForeignKey("wallets.id"))
    to_wallet = Column(UUID(as_uuid=True), ForeignKey("wallets.id"))
    amount = Column(BigInteger, nullable=False)
    currency = _enum_column(CurrencyType, nullable=False, default=CurrencyType.CREDITS)
    status = _enum_column(TransactionStatus, default=TransactionStatus.PENDING)
    type = _enum_column(TransactionType, nullable=False)
    task_session_id = Column(UUID(as_uuid=True), ForeignKey("task_sessions.id"))
    platform_fee = Column(BigInteger, default=0)
    platform_fee_rate = Column(Numeric(5, 4), default=0.025)
    extra_data = Column(JSON, default={})
    idempotency_key = Column(String(64), unique=True, nullable=True, index=True)
    created_at = Column(NaiveTimestamp, server_default=func.now())
    completed_at = Column(NaiveTimestamp)


# Span status enum
class SpanStatus(str, enum.Enum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"


# Span model (for reputation computation)
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
    credits_used = Column(BigInteger)
    extra_data = Column(JSON, default={})
    created_at = Column(NaiveTimestamp, server_default=func.now())


# ─────────────────────────────────────────────────────────
# Phase: agent-goals-and-self-improvement
# Worker only writes ImprovementProposal rows (it never reads/writes
# Goal or MemoryItem). Keep the slim model here so the worker package
# stays standalone and doesn't import from the registry.
# ─────────────────────────────────────────────────────────


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


class ImprovementProposal(Base):
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
    status = _enum_column(ProposalStatus, nullable=False, default=ProposalStatus.PROPOSED)
    target_scope = _enum_column(ProposalScope, nullable=False, default=ProposalScope.AGENT)
    importance = Column(Integer, nullable=False, default=50)
    source_task_id = Column(UUID(as_uuid=True), ForeignKey("task_sessions.id", ondelete="SET NULL"))
    converted_task_id = Column(UUID(as_uuid=True), ForeignKey("task_sessions.id", ondelete="SET NULL"))
    created_at = Column(TzTimestamp, nullable=False, server_default=func.now())
    updated_at = Column(TzTimestamp, nullable=False, server_default=func.now(), onupdate=func.now())
