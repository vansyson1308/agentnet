"""ORM for the Maintenance OS tables (DDL: ``app/maintenance/schema_sql.py``).

Mirrors the DDL exactly (``tests/test_db_parity.py`` compares every registry
ORM table with the bootstrapped database). Imported at the end of
``app/models.py`` so ``Base.metadata`` always contains these tables.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func, text

from ..database import Base

TZ = DateTime(timezone=True)
_OBJ = text("'{}'::jsonb")
_ARR = text("'[]'::jsonb")


class MaintenanceIncident(Base):
    __tablename__ = "maintenance_incidents"

    id = Column(UUID(as_uuid=True), primary_key=True)
    fingerprint = Column(String(64), nullable=False)
    incident_class = Column(String(32), nullable=False)
    priority = Column(String(2), nullable=False)
    severity = Column(String(16), nullable=False)
    source = Column(String(64), nullable=False)
    target = Column(String(32), nullable=False)
    desired_state_ref = Column(String(160), nullable=False)
    affected_surfaces = Column(JSONB, nullable=False, server_default=_ARR)
    status = Column(String(16), nullable=False, server_default=text("'open'"))
    first_observed_at = Column(TZ, nullable=False)
    last_observed_at = Column(TZ, nullable=False)
    observation_count = Column(Integer, nullable=False, server_default=text("1"))
    healthy_streak = Column(Integer, nullable=False, server_default=text("0"))
    current_evidence_digest = Column(String(64), nullable=False)
    recurrence_of = Column(UUID(as_uuid=True), ForeignKey("maintenance_incidents.id", ondelete="SET NULL"))
    recurrence_count = Column(Integer, nullable=False, server_default=text("0"))
    case_count = Column(Integer, nullable=False, server_default=text("0"))
    last_case_terminal_at = Column(TZ)
    provenance = Column(JSONB, nullable=False, server_default=_OBJ)
    opened_at = Column(TZ, nullable=False, server_default=func.now())
    resolved_at = Column(TZ)
    closed_reason = Column(String(64))

    __table_args__ = (Index("idx_maintenance_incidents_fingerprint", "fingerprint", "opened_at"),)


class MaintenanceObservation(Base):
    __tablename__ = "maintenance_observations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    incident_id = Column(UUID(as_uuid=True), ForeignKey("maintenance_incidents.id", ondelete="SET NULL"))
    sli = Column(String(64), nullable=False)
    target = Column(String(32), nullable=False)
    source = Column(String(64), nullable=False)
    collector_version = Column(String(32), nullable=False)
    trust_class = Column(String(24), nullable=False)
    ok = Column(Boolean, nullable=False)
    fingerprint = Column(String(64))
    digest = Column(String(64), nullable=False)
    payload = Column(JSONB, nullable=False, server_default=_OBJ)
    observed_at = Column(TZ, nullable=False, server_default=func.now())


class RepairCase(Base):
    __tablename__ = "repair_cases"

    id = Column(UUID(as_uuid=True), primary_key=True)
    incident_id = Column(UUID(as_uuid=True), ForeignKey("maintenance_incidents.id", ondelete="CASCADE"), nullable=False)
    state = Column(String(32), nullable=False)
    priority = Column(String(2), nullable=False)
    risk_class = Column(String(24))
    repair_class = Column(String(32), nullable=False)
    current_plan_revision = Column(Integer, nullable=False, server_default=text("0"))
    attempt_count = Column(Integer, nullable=False, server_default=text("0"))
    rescope_count = Column(Integer, nullable=False, server_default=text("0"))
    state_tries = Column(Integer, nullable=False, server_default=text("0"))
    model_cost_usd = Column(Numeric(12, 6), nullable=False, server_default=text("0"))
    model_calls = Column(Integer, nullable=False, server_default=text("0"))
    started_at = Column(TZ, nullable=False, server_default=func.now())
    state_entered_at = Column(TZ, nullable=False, server_default=func.now())
    next_action_at = Column(TZ)
    deadline_at = Column(TZ)
    case_deadline_at = Column(TZ, nullable=False)
    lease_owner = Column(String(128))
    lease_expires_at = Column(TZ)
    terminal_reason = Column(String(64))
    terminal_at = Column(TZ)
    resumable = Column(Boolean, nullable=False, server_default=text("false"))
    escalation = Column(JSONB, nullable=False, server_default=_OBJ)
    candidate_id = Column(UUID(as_uuid=True), ForeignKey("code_candidates.id", ondelete="SET NULL"))
    promotion_id = Column(UUID(as_uuid=True), ForeignKey("code_promotions.id", ondelete="SET NULL"))
    release_id = Column(UUID(as_uuid=True))
    base_sha = Column(String(64))
    head_sha = Column(String(64))
    merged_sha = Column(String(64))
    facts = Column(JSONB, nullable=False, server_default=_OBJ)
    version = Column(Integer, nullable=False, server_default=text("0"))
    created_at = Column(TZ, nullable=False, server_default=func.now())
    updated_at = Column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (Index("idx_repair_cases_state", "state", "priority"),)


class RepairPlanRevision(Base):
    __tablename__ = "repair_plan_revisions"

    id = Column(UUID(as_uuid=True), primary_key=True)
    case_id = Column(UUID(as_uuid=True), ForeignKey("repair_cases.id", ondelete="CASCADE"), nullable=False)
    revision = Column(Integer, nullable=False)
    parent_revision = Column(Integer)
    files_allowed = Column(JSONB, nullable=False, server_default=_ARR)
    acceptance_tests = Column(JSONB, nullable=False, server_default=_ARR)
    contract_refs = Column(JSONB, nullable=False, server_default=_ARR)
    root_cause = Column(Text, nullable=False, server_default=text("''"))
    approach = Column(Text, nullable=False, server_default=text("''"))
    risk_class = Column(String(24), nullable=False)
    risk_reasons = Column(JSONB, nullable=False, server_default=_ARR)
    rescope_reason = Column(String(64))
    author_activity_id = Column(UUID(as_uuid=True))
    digest = Column(String(64), nullable=False)
    created_at = Column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (Index("uq_repair_plan_revisions_case_revision", "case_id", "revision", unique=True),)


class RepairAttempt(Base):
    __tablename__ = "repair_attempts"

    id = Column(UUID(as_uuid=True), primary_key=True)
    case_id = Column(UUID(as_uuid=True), ForeignKey("repair_cases.id", ondelete="CASCADE"), nullable=False)
    attempt = Column(Integer, nullable=False)
    plan_revision = Column(Integer, nullable=False)
    base_sha = Column(String(64))
    head_sha = Column(String(64))
    patch_digest = Column(String(64))
    turns = Column(Integer, nullable=False, server_default=text("0"))
    test_runs = Column(Integer, nullable=False, server_default=text("0"))
    outcome = Column(String(24), nullable=False, server_default=text("'running'"))
    feedback = Column(JSONB, nullable=False, server_default=_OBJ)
    started_at = Column(TZ, nullable=False, server_default=func.now())
    finished_at = Column(TZ)

    __table_args__ = (Index("uq_repair_attempts_case_attempt", "case_id", "attempt", unique=True),)


class RepairActivity(Base):
    __tablename__ = "repair_activities"

    id = Column(UUID(as_uuid=True), primary_key=True)
    case_id = Column(UUID(as_uuid=True), ForeignKey("repair_cases.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(32), nullable=False)
    role = Column(String(32), nullable=False)
    plan_revision = Column(Integer, nullable=False, server_default=text("0"))
    attempt = Column(Integer, nullable=False, server_default=text("0"))
    try_number = Column(Integer, nullable=False, server_default=text("1"))
    idempotency_key = Column(String(200), nullable=False)
    status = Column(String(16), nullable=False, server_default=text("'running'"))
    input_digest = Column(String(64), nullable=False)
    output_digest = Column(String(64))
    artifact_id = Column(UUID(as_uuid=True))
    error_class = Column(String(48))
    error = Column(String(500))
    model_provider = Column(String(32))
    model_name = Column(String(64))
    tokens_in = Column(Integer, nullable=False, server_default=text("0"))
    tokens_out = Column(Integer, nullable=False, server_default=text("0"))
    cost_usd = Column(Numeric(12, 6), nullable=False, server_default=text("0"))
    turns = Column(Integer, nullable=False, server_default=text("0"))
    lease_owner = Column(String(128))
    lease_expires_at = Column(TZ)
    started_at = Column(TZ, nullable=False, server_default=func.now())
    finished_at = Column(TZ)

    __table_args__ = (
        Index("uq_repair_activities_idempotency", "idempotency_key", unique=True),
        Index("idx_repair_activities_case", "case_id", "kind", "started_at"),
        Index("idx_repair_activities_day", "started_at"),
    )


class RepairArtifact(Base):
    __tablename__ = "repair_artifacts"

    id = Column(UUID(as_uuid=True), primary_key=True)
    case_id = Column(UUID(as_uuid=True), ForeignKey("repair_cases.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(32), nullable=False)
    digest = Column(String(64), nullable=False)
    trust_class = Column(String(24), nullable=False)
    plan_revision = Column(Integer, nullable=False, server_default=text("0"))
    attempt = Column(Integer, nullable=False, server_default=text("0"))
    produced_by = Column(String(128), nullable=False)
    content = Column(JSONB, nullable=False, server_default=_OBJ)
    created_at = Column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (Index("uq_repair_artifacts_case_kind_digest", "case_id", "kind", "digest", unique=True),)


class RepairEvidence(Base):
    __tablename__ = "repair_evidence"

    id = Column(UUID(as_uuid=True), primary_key=True)
    case_id = Column(UUID(as_uuid=True), ForeignKey("repair_cases.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(48), nullable=False)
    source = Column(String(64), nullable=False)
    collector_version = Column(String(32), nullable=False)
    trust_class = Column(String(24), nullable=False)
    digest = Column(String(64), nullable=False)
    payload = Column(JSONB, nullable=False, server_default=_OBJ)
    collected_at = Column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (Index("uq_repair_evidence_case_digest", "case_id", "kind", "digest", unique=True),)


class RepairTransition(Base):
    __tablename__ = "repair_transitions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    case_id = Column(UUID(as_uuid=True), ForeignKey("repair_cases.id", ondelete="CASCADE"), nullable=False)
    from_state = Column(String(32))
    to_state = Column(String(32), nullable=False)
    actor_type = Column(String(16), nullable=False)
    actor_id = Column(String(128), nullable=False)
    reason_code = Column(String(64), nullable=False)
    evidence_digest = Column(String(64))
    detail = Column(JSONB, nullable=False, server_default=_OBJ)
    created_at = Column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (Index("idx_repair_transitions_case", "case_id", "id"),)


class MaintenanceRelease(Base):
    __tablename__ = "maintenance_releases"

    id = Column(UUID(as_uuid=True), primary_key=True)
    case_id = Column(UUID(as_uuid=True), ForeignKey("repair_cases.id", ondelete="CASCADE"), nullable=False)
    incident_id = Column(UUID(as_uuid=True), ForeignKey("maintenance_incidents.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(24), nullable=False, server_default=text("'pending'"))
    risk_class = Column(String(24), nullable=False)
    head_sha = Column(String(64), nullable=False)
    tree_sha = Column(String(64))
    attestation = Column(JSONB, nullable=False, server_default=_OBJ)
    attestation_digest = Column(String(64), nullable=False)
    attestation_signature = Column(String(128), nullable=False)
    verification = Column(JSONB, nullable=False, server_default=_OBJ)
    services = Column(JSONB, nullable=False, server_default=_ARR)
    release_branch = Column(String(255))
    pr_number = Column(Integer)
    pr_url = Column(String(512))
    production_merge_sha = Column(String(64))
    deployments = Column(JSONB, nullable=False, server_default=_OBJ)
    known_good_id = Column(UUID(as_uuid=True))
    rollback = Column(JSONB, nullable=False, server_default=_OBJ)
    healthy_streak = Column(Integer, nullable=False, server_default=text("0"))
    failure_reason = Column(String(500))
    attempt = Column(Integer, nullable=False, server_default=text("0"))
    lease_owner = Column(String(128))
    lease_expires_at = Column(TZ)
    next_action_at = Column(TZ, nullable=False, server_default=func.now())
    deadline_at = Column(TZ, nullable=False)
    created_at = Column(TZ, nullable=False, server_default=func.now())
    updated_at = Column(TZ, nullable=False, server_default=func.now())
    completed_at = Column(TZ)

    __table_args__ = (Index("uq_maintenance_releases_case_sha", "case_id", "head_sha", unique=True),)


class MaintenanceKnownGood(Base):
    __tablename__ = "maintenance_known_good"

    id = Column(UUID(as_uuid=True), primary_key=True)
    environment = Column(String(32), nullable=False)
    production_sha = Column(String(64), nullable=False)
    tree_sha = Column(String(64))
    deployments = Column(JSONB, nullable=False, server_default=_OBJ)
    config_digest = Column(String(64))
    evidence = Column(JSONB, nullable=False, server_default=_OBJ)
    source = Column(String(64), nullable=False)
    recorded_at = Column(TZ, nullable=False, server_default=func.now())
    retired_at = Column(TZ)

    __table_args__ = (Index("idx_maintenance_known_good_env", "environment", "recorded_at"),)


class MaintenanceReleaseFreeze(Base):
    __tablename__ = "maintenance_release_freezes"

    id = Column(UUID(as_uuid=True), primary_key=True)
    reason_code = Column(String(64), nullable=False)
    detail = Column(JSONB, nullable=False, server_default=_OBJ)
    owner_only = Column(Boolean, nullable=False, server_default=text("true"))
    opened_by = Column(String(128), nullable=False)
    opened_at = Column(TZ, nullable=False, server_default=func.now())
    lifted_at = Column(TZ)
    lifted_by = Column(String(128))
    lift_reason = Column(String(255))

    __table_args__ = (Index("idx_maintenance_release_freezes_open", "lifted_at"),)


class MaintenanceToilEvent(Base):
    __tablename__ = "maintenance_toil_events"

    id = Column(UUID(as_uuid=True), primary_key=True)
    kind = Column(String(40), nullable=False)
    case_id = Column(UUID(as_uuid=True), ForeignKey("repair_cases.id", ondelete="SET NULL"))
    actor = Column(String(128), nullable=False)
    detail = Column(JSONB, nullable=False, server_default=_OBJ)
    created_at = Column(TZ, nullable=False, server_default=func.now())


class MaintenanceHeartbeat(Base):
    __tablename__ = "maintenance_heartbeats"

    component = Column(String(48), primary_key=True)
    worker_id = Column(String(128), nullable=False)
    beat_at = Column(TZ, nullable=False, server_default=func.now())
    cycles = Column(BigInteger, nullable=False, server_default=text("0"))
    errors = Column(BigInteger, nullable=False, server_default=text("0"))
    last_error_class = Column(String(64))
    last_error_at = Column(TZ)
    details = Column(JSONB, nullable=False, server_default=_OBJ)


class MaintenanceKnowledge(Base):
    __tablename__ = "maintenance_knowledge"

    id = Column(UUID(as_uuid=True), primary_key=True)
    case_id = Column(UUID(as_uuid=True), ForeignKey("repair_cases.id", ondelete="CASCADE"), nullable=False)
    incident_fingerprint = Column(String(64), nullable=False)
    incident_class = Column(String(32), nullable=False)
    outcome = Column(String(32), nullable=False)
    root_cause = Column(Text, nullable=False, server_default=text("''"))
    repair_digest = Column(String(64))
    tests_added = Column(JSONB, nullable=False, server_default=_ARR)
    monitors = Column(JSONB, nullable=False, server_default=_ARR)
    release_result = Column(String(32))
    rollback_result = Column(String(32))
    detector_gap = Column(JSONB, nullable=False, server_default=_OBJ)
    created_at = Column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("uq_maintenance_knowledge_case", "case_id", unique=True),
        Index("idx_maintenance_knowledge_fingerprint", "incident_fingerprint"),
    )
