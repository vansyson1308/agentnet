"""Payment service ORM.

These models are a *faithful subset* of the shared PostgreSQL schema that
``services/registry/init-db/*.sql`` creates (see
docs/DATABASE_SCHEMA_CONTRACT.md):

* every column declared here has the same name / type / nullability as the
  database column;
* a column the payment service never touches may be omitted ONLY if it is
  nullable or has a DB default (otherwise inserts from this service would
  fail) — ``tests/test_db_parity.py`` enforces both rules;
* ``approval_requests`` is owned by this service; its DDL lives in
  ``services/registry/app/schema_app_sql.py`` (migration 0009 /
  init-db/17-app-tables.sql).
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

    ``native_enum=False``: the DB enum types are created by init-db and are
    the source of truth; the ORM only binds string values.
    """
    return Column(Enum(enum_cls, native_enum=False, values_callable=lambda x: [e.value for e in x]), **kwargs)


# Core tables (users/agents/wallets/task_sessions/transactions) use naive
# TIMESTAMP in the DB; approval_requests (created later) uses TIMESTAMPTZ.
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


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


# User model
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
    total_tasks_completed = Column(Integer, default=0)
    total_tasks_failed = Column(Integer, default=0)
    total_tasks_timeout = Column(Integer, default=0)
    success_rate = Column(Float, default=0.0)
    avg_response_time_ms = Column(Integer, default=0)
    total_volume_credits = Column(BigInteger, default=0)
    reputation_tier = Column(String, default="unranked")
    reputation_updated_at = Column(NaiveTimestamp)
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
    currency = _enum_column(CurrencyType, nullable=False, default="credits")
    status = _enum_column(TaskStatus, default="initiated")
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
    currency = _enum_column(CurrencyType, nullable=False, default="credits")
    status = _enum_column(TransactionStatus, default="pending")
    type = _enum_column(TransactionType, nullable=False)
    task_session_id = Column(UUID(as_uuid=True), ForeignKey("task_sessions.id"))
    platform_fee = Column(BigInteger, default=0)
    platform_fee_rate = Column(Numeric(5, 4), default=0.025)
    extra_data = Column(JSON, default={})
    idempotency_key = Column(String(64), unique=True, nullable=True, index=True)
    created_at = Column(NaiveTimestamp, server_default=func.now())
    completed_at = Column(NaiveTimestamp)


# ApprovalRequest model — owned by the payment service. DDL:
# services/registry/app/schema_app_sql.py (single source), executed by
# migration 0009_app_tables and shipped as init-db/17-app-tables.sql.
class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    # BIGINT like every other money column (Python still sees an int).
    amount = Column(BigInteger, nullable=False)
    currency = _enum_column(CurrencyType, nullable=False, default="credits")
    description = Column(Text, nullable=False)
    callback_url = Column(String)
    status = _enum_column(ApprovalStatus, default="pending")
    created_at = Column(TzTimestamp, server_default=func.now())
    updated_at = Column(TzTimestamp, server_default=func.now(), onupdate=func.now())
    responded_at = Column(TzTimestamp)

    # Fields for task escrow payment approvals
    task_session_id = Column(UUID(as_uuid=True), ForeignKey("task_sessions.id"), nullable=True)
    expires_at = Column(TzTimestamp, nullable=True)
    approved_at = Column(TzTimestamp, nullable=True)
    denied_at = Column(TzTimestamp, nullable=True)
