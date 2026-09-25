"""ORM for the A2A integration tables (DDL: ``app/a2a/schema_sql.py``).

The ORM mirrors the DDL exactly (``tests/test_db_parity.py`` compares every
registry ORM table with the bootstrapped database). Imported at the end of
``app/models.py`` so ``Base.metadata`` always contains these tables.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func, text

from ..database import Base

TZ = DateTime(timezone=True)


class A2ATask(Base):
    __tablename__ = "a2a_tasks"

    id = Column(UUID(as_uuid=True), primary_key=True)
    context_id = Column(UUID(as_uuid=True), nullable=False)
    tenant_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"))
    caller_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"))
    caller_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    task_session_id = Column(UUID(as_uuid=True), ForeignKey("task_sessions.id", ondelete="SET NULL"))
    skill_id = Column(String(128))
    state = Column(String(32), nullable=False)
    status_message = Column(JSONB)
    status_timestamp = Column(TZ, nullable=False, server_default=func.now())
    protocol_version = Column(String(8), nullable=False, server_default=text("'1.0'"))
    binding = Column(String(16), nullable=False)
    economics = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    metadata_ = Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    idempotency_key = Column(String(255), nullable=False)
    federation_depth = Column(SmallInteger, nullable=False, server_default=text("0"))
    last_event_seq = Column(Integer, nullable=False, server_default=text("0"))
    created_at = Column(TZ, nullable=False, server_default=func.now())
    updated_at = Column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (Index("uq_a2a_tasks_idempotency", "idempotency_key", unique=True),)


class A2AMessage(Base):
    __tablename__ = "a2a_messages"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_id = Column(UUID(as_uuid=True), ForeignKey("a2a_tasks.id", ondelete="CASCADE"), nullable=False)
    context_id = Column(UUID(as_uuid=True), nullable=False)
    message_id = Column(String(128), nullable=False)
    role = Column(String(16), nullable=False)
    message = Column(JSONB, nullable=False)
    created_at = Column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (Index("uq_a2a_messages_task_message", "task_id", "message_id", unique=True),)


class A2AArtifact(Base):
    __tablename__ = "a2a_artifacts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_id = Column(UUID(as_uuid=True), ForeignKey("a2a_tasks.id", ondelete="CASCADE"), nullable=False)
    artifact_id = Column(String(128), nullable=False)
    artifact = Column(JSONB, nullable=False)
    created_at = Column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (Index("uq_a2a_artifacts_task_artifact", "task_id", "artifact_id", unique=True),)


class A2ATaskEvent(Base):
    __tablename__ = "a2a_task_events"

    task_id = Column(UUID(as_uuid=True), ForeignKey("a2a_tasks.id", ondelete="CASCADE"), primary_key=True)
    seq = Column(Integer, primary_key=True, autoincrement=False)
    kind = Column(String(16), nullable=False)
    payload = Column(JSONB, nullable=False)
    created_at = Column(TZ, nullable=False, server_default=func.now())


class A2AAuditLog(Base):
    __tablename__ = "a2a_audit_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    created_at = Column(TZ, nullable=False, server_default=func.now())
    principal_class = Column(String(16), nullable=False)
    principal_id = Column(UUID(as_uuid=True))
    target_agent_id = Column(UUID(as_uuid=True))
    operation = Column(String(48), nullable=False)
    a2a_task_id = Column(UUID(as_uuid=True))
    task_session_id = Column(UUID(as_uuid=True))
    result = Column(String(48), nullable=False)
    economics_action = Column(String(32))
    request_id = Column(String(64))
    detail = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class A2ARemoteAgent(Base):
    __tablename__ = "a2a_remote_agents"

    id = Column(UUID(as_uuid=True), primary_key=True)
    card_url = Column(String(2048), nullable=False)
    host = Column(String(255), nullable=False)
    name = Column(String(255))
    description = Column(Text)
    provider_org = Column(String(255))
    provider_url = Column(String(2048))
    card_version = Column(String(64))
    card_hash = Column(String(64))
    etag = Column(String(255))
    protocol_versions = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    bindings = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    skills = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    security = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    capabilities = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    state = Column(String(16), nullable=False, server_default=text("'discovered'"))
    state_reason = Column(String(255))
    last_fetch_at = Column(TZ)
    last_validated_at = Column(TZ)
    last_error_class = Column(String(64))
    consecutive_failures = Column(Integer, nullable=False, server_default=text("0"))
    provenance = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(TZ, nullable=False, server_default=func.now())
    updated_at = Column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (Index("uq_a2a_remote_agents_card_url", "card_url", unique=True),)


class A2ARemoteCardVersion(Base):
    __tablename__ = "a2a_remote_card_versions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    remote_agent_id = Column(UUID(as_uuid=True), ForeignKey("a2a_remote_agents.id", ondelete="CASCADE"), nullable=False)
    card_hash = Column(String(64), nullable=False)
    card = Column(JSONB, nullable=False)
    material_change = Column(Boolean, nullable=False, server_default=text("false"))
    change_summary = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    fetched_at = Column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (Index("uq_a2a_remote_card_versions", "remote_agent_id", "card_hash", unique=True),)


class A2AConnection(Base):
    __tablename__ = "a2a_connections"

    id = Column(UUID(as_uuid=True), primary_key=True)
    remote_agent_id = Column(UUID(as_uuid=True), ForeignKey("a2a_remote_agents.id", ondelete="CASCADE"), nullable=False)
    label = Column(String(128), nullable=False)
    auth_scheme = Column(String(16), nullable=False, server_default=text("'none'"))
    sealed_credential = Column(Text)
    daily_call_limit = Column(Integer, nullable=False, server_default=text("20"))
    created_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(TZ, nullable=False, server_default=func.now())
    revoked_at = Column(TZ)


class A2AOutboundCall(Base):
    __tablename__ = "a2a_outbound_calls"

    id = Column(UUID(as_uuid=True), primary_key=True)
    connection_id = Column(UUID(as_uuid=True), ForeignKey("a2a_connections.id", ondelete="SET NULL"))
    remote_agent_id = Column(UUID(as_uuid=True), ForeignKey("a2a_remote_agents.id", ondelete="SET NULL"))
    initiator_class = Column(String(16), nullable=False)
    initiator_id = Column(UUID(as_uuid=True))
    correlation_id = Column(UUID(as_uuid=True))
    causation_id = Column(UUID(as_uuid=True))
    intent_id = Column(UUID(as_uuid=True))
    depth = Column(SmallInteger, nullable=False, server_default=text("1"))
    operation = Column(String(32), nullable=False)
    skill_id = Column(String(128))
    idempotency_key = Column(String(255), nullable=False)
    remote_task_id = Column(String(255))
    remote_context_id = Column(String(255))
    remote_state = Column(String(32))
    status = Column(String(16), nullable=False, server_default=text("'pending'"))
    error_class = Column(String(64))
    result_summary = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(TZ, nullable=False, server_default=func.now())
    finished_at = Column(TZ)

    __table_args__ = (Index("uq_a2a_outbound_calls_idempotency", "idempotency_key", unique=True),)
