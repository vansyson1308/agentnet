"""Persistent execution-plane models for the managed execution path.

These tables deliberately do not duplicate Paperclip's WorkItem/DAG model.
They persist immutable external references plus AgentNet-owned runtime,
allocation, delivery, run, and artifact state.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func, text

from .database import Base


def _enum_column(enum_cls, **kwargs):
    return Column(
        Enum(enum_cls, native_enum=False, values_callable=lambda values: [item.value for item in values]),
        **kwargs,
    )


class ExecutionMode(str, enum.Enum):
    LEGACY = "legacy"
    MANAGED_SHADOW = "managed_shadow"
    MANAGED_VALUE = "managed_value"


class RuntimeStatus(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DRAINING = "draining"


class ManagedExecutionStatus(str, enum.Enum):
    REQUESTED = "requested"
    ALLOCATING = "allocating"
    ACTIVE = "active"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStatus(str, enum.Enum):
    CREATED = "created"
    DISPATCHED = "dispatched"
    ACKNOWLEDGED = "acknowledged"
    RUNNING = "running"
    ARTIFACT_SUBMITTED = "artifact_submitted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class AttemptKind(str, enum.Enum):
    INITIAL = "initial"
    REPAIR = "repair"
    QA = "qa"


class AttemptStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LeaseStatus(str, enum.Enum):
    OFFERED = "offered"
    ACKNOWLEDGED = "acknowledged"
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class OutboxStatus(str, enum.Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    DEAD_LETTER = "dead_letter"


class Runtime(Base):
    __tablename__ = "runtimes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    registration_key = Column(String(255), nullable=False, unique=True)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"))
    name = Column(String(255), nullable=False)
    role = Column(String(64), nullable=False)
    adapter = Column(String(64), nullable=False)
    capabilities = Column(PG_JSONB, nullable=False, default=list)
    repository_scopes = Column(PG_JSONB, nullable=False, default=list)
    permissions = Column(PG_JSONB, nullable=False, default=dict)
    model = Column(String(128))
    provider = Column(String(128))
    capacity = Column(Integer, nullable=False, default=1)
    token_hash = Column(String(64), nullable=False, unique=True)
    status = _enum_column(RuntimeStatus, nullable=False, default=RuntimeStatus.ONLINE)
    last_heartbeat_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    timeout_count = Column(Integer, nullable=False, default=0)
    error_count = Column(Integer, nullable=False, default=0)
    extra_data = Column(PG_JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    slots = relationship("RuntimeSlot", back_populates="runtime", cascade="all, delete-orphan")


class RuntimeSlot(Base):
    __tablename__ = "runtime_slots"
    __table_args__ = (UniqueConstraint("runtime_id", "slot_number", name="uq_runtime_slot_number"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    runtime_id = Column(UUID(as_uuid=True), ForeignKey("runtimes.id", ondelete="CASCADE"), nullable=False)
    slot_number = Column(Integer, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    runtime = relationship("Runtime", back_populates="slots")


class RuntimeHeartbeat(Base):
    __tablename__ = "runtime_heartbeats"
    __table_args__ = (UniqueConstraint("runtime_id", "sequence", name="uq_runtime_heartbeat_sequence"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    runtime_id = Column(UUID(as_uuid=True), ForeignKey("runtimes.id", ondelete="CASCADE"), nullable=False)
    sequence = Column(Integer, nullable=False)
    run_id = Column(UUID(as_uuid=True), ForeignKey("execution_runs.id", ondelete="SET NULL"))
    lease_id = Column(UUID(as_uuid=True), ForeignKey("leases.id", ondelete="SET NULL"))
    resources = Column(PG_JSONB, nullable=False, default=dict)
    recorded_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ManagedExecution(Base):
    __tablename__ = "managed_executions"
    __table_args__ = (
        UniqueConstraint(
            "control_plane",
            "work_item_id",
            "work_item_revision",
            "external_attempt_no",
            "role",
            name="uq_managed_execution_work_attempt_role",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    control_plane = Column(String(64), nullable=False, default="paperclip")
    goal_id = Column(String(255), nullable=False)
    work_item_id = Column(String(255), nullable=False)
    work_item_revision = Column(String(128), nullable=False)
    external_attempt_no = Column(Integer, nullable=False, default=1)
    task_session_id = Column(
        UUID(as_uuid=True), ForeignKey("task_sessions.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    initial_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("execution_runs.id", ondelete="RESTRICT", use_alter=True, name="fk_managed_execution_initial_run"),
        nullable=True,
        unique=True,
    )
    idempotency_key = Column(String(255), nullable=False, unique=True)
    request_hash = Column(String(64), nullable=False)
    execution_mode = _enum_column(ExecutionMode, nullable=False, default=ExecutionMode.MANAGED_SHADOW)
    status = _enum_column(ManagedExecutionStatus, nullable=False, default=ManagedExecutionStatus.REQUESTED)
    role = Column(String(64), nullable=False)
    capability = Column(String(255), nullable=False)
    priority = Column(Integer, nullable=False, default=50)
    repository = Column(String(1024), nullable=False)
    base_commit_sha = Column(String(64), nullable=False)
    repository_scope = Column(PG_JSONB, nullable=False, default=list)
    prompt = Column(Text, nullable=False)
    acceptance_snapshot = Column(PG_JSONB, nullable=False)
    requirements = Column(PG_JSONB, nullable=False, default=dict)
    budgets = Column(PG_JSONB, nullable=False, default=dict)
    approval_policy_version = Column(String(128), nullable=False)
    required_runtime_id = Column(UUID(as_uuid=True), ForeignKey("runtimes.id", ondelete="RESTRICT"))
    trace_id = Column(UUID(as_uuid=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class ExecutionRun(Base):
    __tablename__ = "execution_runs"
    __table_args__ = (
        UniqueConstraint("managed_execution_id", "run_number", "role", name="uq_execution_run_number_role"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    managed_execution_id = Column(
        UUID(as_uuid=True), ForeignKey("managed_executions.id", ondelete="CASCADE"), nullable=False
    )
    task_session_id = Column(UUID(as_uuid=True), ForeignKey("task_sessions.id", ondelete="RESTRICT"), nullable=False)
    runtime_id = Column(UUID(as_uuid=True), ForeignKey("runtimes.id", ondelete="RESTRICT"), nullable=False)
    run_number = Column(Integer, nullable=False, default=1)
    role = Column(String(64), nullable=False)
    capability = Column(String(255), nullable=False)
    repository = Column(String(1024), nullable=False)
    base_commit_sha = Column(String(64), nullable=False)
    candidate_commit_sha = Column(String(64))
    prompt_snapshot = Column(Text, nullable=False)
    acceptance_snapshot = Column(PG_JSONB, nullable=False)
    budgets = Column(PG_JSONB, nullable=False, default=dict)
    status = _enum_column(RunStatus, nullable=False, default=RunStatus.CREATED)
    event_sequence = Column(Integer, nullable=False, default=0)
    acknowledged_at = Column(DateTime(timezone=True))
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    deadline_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("run_id", "attempt_number", name="uq_attempt_run_number"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(UUID(as_uuid=True), ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False)
    attempt_number = Column(Integer, nullable=False)
    kind = _enum_column(AttemptKind, nullable=False, default=AttemptKind.INITIAL)
    status = _enum_column(AttemptStatus, nullable=False, default=AttemptStatus.PENDING)
    retry_of_id = Column(UUID(as_uuid=True), ForeignKey("attempts.id", ondelete="SET NULL"))
    failure_signature = Column(String(128))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at = Column(DateTime(timezone=True))


class Lease(Base):
    __tablename__ = "leases"
    __table_args__ = (
        Index(
            "uq_active_lease_per_run",
            "run_id",
            unique=True,
            postgresql_where=text("state IN ('offered', 'acknowledged', 'active')"),
        ),
        Index(
            "uq_active_lease_per_runtime_slot",
            "runtime_slot_id",
            unique=True,
            postgresql_where=text("state IN ('offered', 'acknowledged', 'active')"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(UUID(as_uuid=True), ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False)
    attempt_id = Column(UUID(as_uuid=True), ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False)
    runtime_slot_id = Column(UUID(as_uuid=True), ForeignKey("runtime_slots.id", ondelete="RESTRICT"), nullable=False)
    state = _enum_column(LeaseStatus, nullable=False, default=LeaseStatus.OFFERED)
    token_hash = Column(String(64), unique=True)
    offered_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    acknowledged_at = Column(DateTime(timezone=True))
    heartbeat_at = Column(DateTime(timezone=True))
    expires_at = Column(DateTime(timezone=True), nullable=False)
    released_at = Column(DateTime(timezone=True))


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),
        UniqueConstraint("run_id", "idempotency_key", name="uq_run_event_idempotency"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(UUID(as_uuid=True), ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False)
    sequence = Column(Integer, nullable=False)
    event_type = Column(String(128), nullable=False)
    idempotency_key = Column(String(255), nullable=False)
    trace_id = Column(UUID(as_uuid=True), nullable=False)
    payload = Column(PG_JSONB, nullable=False, default=dict)
    occurred_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("run_id", "artifact_type", "sha256", name="uq_run_artifact_hash"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(UUID(as_uuid=True), ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False)
    artifact_type = Column(String(64), nullable=False)
    uri = Column(String(2048), nullable=False)
    sha256 = Column(String(64), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    mime_type = Column(String(255), nullable=False)
    base_commit_sha = Column(String(64))
    candidate_commit_sha = Column(String(64))
    manifest = Column(PG_JSONB, nullable=False, default=dict)
    changed_files = Column(PG_JSONB, nullable=False, default=list)
    provenance = Column(PG_JSONB, nullable=False, default=dict)
    usage = Column(PG_JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class IntegrationOutbox(Base):
    __tablename__ = "integration_outbox"
    __table_args__ = (UniqueConstraint("aggregate_id", "sequence", name="uq_outbox_aggregate_sequence"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID(as_uuid=True), nullable=False, unique=True, default=uuid.uuid4)
    event_type = Column(String(128), nullable=False)
    schema_version = Column(String(16), nullable=False, default="1")
    aggregate_id = Column(UUID(as_uuid=True), nullable=False)
    sequence = Column(Integer, nullable=False)
    idempotency_key = Column(String(255), nullable=False, unique=True)
    trace_id = Column(UUID(as_uuid=True), nullable=False)
    payload = Column(PG_JSONB, nullable=False, default=dict)
    state = _enum_column(OutboxStatus, nullable=False, default=OutboxStatus.PENDING)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    occurred_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    delivered_at = Column(DateTime(timezone=True))
    last_error = Column(Text)
