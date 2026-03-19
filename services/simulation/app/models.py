"""
Simulation service database models.

All tables use the `sim_` prefix.
Money invariant: these tables NEVER store or modify wallet balances.
Escrow is linked via sim_sessions.task_session_id -> task_sessions.id.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from .database import Base


class SimStatus(str, enum.Enum):
    """Simulation lifecycle states."""

    INITIALIZING = "initializing"
    BUILDING_GRAPH = "building_graph"
    GENERATING_AGENTS = "generating_agents"
    RUNNING = "running"
    GENERATING_REPORT = "generating_report"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


# Valid state transitions for the simulation state machine
VALID_SIM_TRANSITIONS = {
    SimStatus.INITIALIZING: [SimStatus.BUILDING_GRAPH, SimStatus.FAILED, SimStatus.CANCELLED],
    SimStatus.BUILDING_GRAPH: [
        SimStatus.GENERATING_AGENTS,
        SimStatus.FAILED,
        SimStatus.CANCELLED,
        SimStatus.TIMEOUT,
    ],
    SimStatus.GENERATING_AGENTS: [
        SimStatus.RUNNING,
        SimStatus.FAILED,
        SimStatus.CANCELLED,
        SimStatus.TIMEOUT,
    ],
    SimStatus.RUNNING: [
        SimStatus.GENERATING_REPORT,
        SimStatus.COMPLETED,
        SimStatus.FAILED,
        SimStatus.CANCELLED,
        SimStatus.TIMEOUT,
    ],
    SimStatus.GENERATING_REPORT: [
        SimStatus.COMPLETED,
        SimStatus.FAILED,
        SimStatus.TIMEOUT,
    ],
    SimStatus.COMPLETED: [],
    SimStatus.FAILED: [],
    SimStatus.CANCELLED: [],
    SimStatus.TIMEOUT: [],
}


def validate_sim_transition(current: SimStatus, target: SimStatus) -> bool:
    """Check if a state transition is valid."""
    return target in VALID_SIM_TRANSITIONS.get(current, [])


class SimSession(Base):
    """A simulation session — one run of the MiroFish pipeline."""

    __tablename__ = "sim_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    task_session_id = Column(UUID(as_uuid=True), nullable=True)  # FK to escrow
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(
        Enum(SimStatus, name="sim_status", create_type=False),
        nullable=False,
        default=SimStatus.INITIALIZING,
    )
    seed_config = Column(JSONB, nullable=False)
    simulation_config = Column(JSONB, nullable=False)
    platform = Column(String(50), nullable=False, default="twitter")
    num_steps = Column(Integer, nullable=False, default=100)
    num_simulated_agents = Column(Integer, default=0)
    cost_credits = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)
    progress_pct = Column(Integer, default=0)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    agent_profiles = relationship("SimAgentProfile", back_populates="session", cascade="all, delete-orphan")
    results = relationship("SimResult", back_populates="session", cascade="all, delete-orphan")
    reports = relationship("SimReport", back_populates="session", cascade="all, delete-orphan")
    chat_messages = relationship("SimChatMessage", back_populates="session", cascade="all, delete-orphan")


class SimAgentProfile(Base):
    """A simulated agent persona generated from AgentNet seed data."""

    __tablename__ = "sim_agent_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sim_session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("sim_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_agent_id = Column(UUID(as_uuid=True), nullable=True)  # NULL if injected
    persona_name = Column(String(255), nullable=False)
    persona_data = Column(JSONB, nullable=False)
    is_injected = Column(Boolean, nullable=False, default=False)
    agent_index = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    session = relationship("SimSession", back_populates="agent_profiles")


class SimResult(Base):
    """A single simulation step result (one agent action)."""

    __tablename__ = "sim_results"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sim_session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("sim_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_number = Column(Integer, nullable=False)
    agent_index = Column(Integer, nullable=False)
    action_type = Column(String(100), nullable=True)
    content = Column(Text, nullable=True)
    metadata_ = Column("metadata", JSONB, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    session = relationship("SimSession", back_populates="results")


class SimReport(Base):
    """A prediction report generated by the ReportAgent."""

    __tablename__ = "sim_reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sim_session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("sim_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    report_type = Column(String(50), nullable=False, default="prediction")
    title = Column(String(500), nullable=True)
    content = Column(Text, nullable=False)
    summary = Column(Text, nullable=True)
    key_findings = Column(JSONB, nullable=True)
    confidence_score = Column(Float, nullable=True)
    metadata_ = Column("metadata", JSONB, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    session = relationship("SimSession", back_populates="reports")


class SimChatMessage(Base):
    """A chat message for post-simulation agent interviews."""

    __tablename__ = "sim_chat_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sim_session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("sim_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_index = Column(Integer, nullable=False)
    role = Column(String(20), nullable=False)  # 'user' or 'agent'
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    session = relationship("SimSession", back_populates="chat_messages")
