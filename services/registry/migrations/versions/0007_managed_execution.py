"""persistent managed execution, runtime, run, attempt and lease model

Revision ID: 0007_managed_execution
Revises: 0006_email_verified
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_managed_execution"
down_revision: Union[str, None] = "0006_email_verified"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "task_sessions",
        sa.Column("execution_mode", sa.String(length=32), nullable=False, server_default="legacy"),
    )
    op.create_check_constraint(
        "ck_managed_shadow_zero_escrow",
        "task_sessions",
        "execution_mode <> 'managed_shadow' OR escrow_amount = 0",
    )
    op.add_column(
        "goals",
        sa.Column("execution_mode", sa.String(length=32), nullable=False, server_default="legacy"),
    )

    op.create_table(
        "runtimes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("registration_key", sa.String(length=255), nullable=False, unique=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="SET NULL")),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("adapter", sa.String(length=64), nullable=False),
        sa.Column("capabilities", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("repository_scopes", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("permissions", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("model", sa.String(length=128)),
        sa.Column("provider", sa.String(length=128)),
        sa.Column("capacity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="online"),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("timeout_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extra_data", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("capacity > 0", name="ck_runtime_positive_capacity"),
    )
    op.create_index("ix_runtime_allocator", "runtimes", ["status", "role", "last_heartbeat_at"])

    op.create_table(
        "runtime_slots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "runtime_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runtimes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slot_number", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("runtime_id", "slot_number", name="uq_runtime_slot_number"),
    )

    op.create_table(
        "managed_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("control_plane", sa.String(length=64), nullable=False, server_default="paperclip"),
        sa.Column("goal_id", sa.String(length=255), nullable=False),
        sa.Column("work_item_id", sa.String(length=255), nullable=False),
        sa.Column("work_item_revision", sa.String(length=128), nullable=False),
        sa.Column("external_attempt_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "task_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("task_sessions.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("initial_run_id", postgresql.UUID(as_uuid=True), unique=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("execution_mode", sa.String(length=32), nullable=False, server_default="managed_shadow"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="requested"),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("capability", sa.String(length=255), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("repository", sa.String(length=1024), nullable=False),
        sa.Column("base_commit_sha", sa.String(length=64), nullable=False),
        sa.Column("repository_scope", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("acceptance_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("requirements", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("budgets", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("approval_policy_version", sa.String(length=128), nullable=False),
        sa.Column(
            "required_runtime_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runtimes.id", ondelete="RESTRICT"),
        ),
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "control_plane",
            "work_item_id",
            "work_item_revision",
            "external_attempt_no",
            "role",
            name="uq_managed_execution_work_attempt_role",
        ),
        sa.CheckConstraint("execution_mode = 'managed_shadow'", name="ck_first_batch_shadow_only"),
    )

    op.create_table(
        "execution_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "managed_execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("managed_executions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("task_sessions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "runtime_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runtimes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("run_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("capability", sa.String(length=255), nullable=False),
        sa.Column("repository", sa.String(length=1024), nullable=False),
        sa.Column("base_commit_sha", sa.String(length=64), nullable=False),
        sa.Column("candidate_commit_sha", sa.String(length=64)),
        sa.Column("prompt_snapshot", sa.Text(), nullable=False),
        sa.Column("acceptance_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("budgets", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="created"),
        sa.Column("event_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("managed_execution_id", "run_number", "role", name="uq_execution_run_number_role"),
    )
    op.create_foreign_key(
        "fk_managed_execution_initial_run",
        "managed_executions",
        "execution_runs",
        ["initial_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("execution_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="initial"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("retry_of_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("attempts.id", ondelete="SET NULL")),
        sa.Column("failure_signature", sa.String(length=128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "attempt_number", name="uq_attempt_run_number"),
    )

    op.create_table(
        "leases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("execution_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "runtime_slot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runtime_slots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="offered"),
        sa.Column("token_hash", sa.String(length=64), unique=True),
        sa.Column("offered_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "uq_active_lease_per_run",
        "leases",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('offered', 'acknowledged', 'active')"),
    )
    op.create_index(
        "uq_active_lease_per_runtime_slot",
        "leases",
        ["runtime_slot_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('offered', 'acknowledged', 'active')"),
    )

    op.create_table(
        "run_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("execution_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),
        sa.UniqueConstraint("run_id", "idempotency_key", name="uq_run_event_idempotency"),
    )

    op.create_table(
        "runtime_heartbeats",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "runtime_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runtimes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("execution_runs.id", ondelete="SET NULL")),
        sa.Column("lease_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("leases.id", ondelete="SET NULL")),
        sa.Column("resources", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("runtime_id", "sequence", name="uq_runtime_heartbeat_sequence"),
    )

    op.create_table(
        "integration_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="1"),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False, unique=True),
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.UniqueConstraint("aggregate_id", "sequence", name="uq_outbox_aggregate_sequence"),
    )
    op.create_index("ix_outbox_pending", "integration_outbox", ["state", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_outbox_pending", table_name="integration_outbox")
    op.drop_table("integration_outbox")
    op.drop_table("runtime_heartbeats")
    op.drop_table("run_events")
    op.drop_index("uq_active_lease_per_runtime_slot", table_name="leases")
    op.drop_index("uq_active_lease_per_run", table_name="leases")
    op.drop_table("leases")
    op.drop_table("attempts")
    op.drop_constraint("fk_managed_execution_initial_run", "managed_executions", type_="foreignkey")
    op.drop_table("execution_runs")
    op.drop_table("managed_executions")
    op.drop_table("runtime_slots")
    op.drop_index("ix_runtime_allocator", table_name="runtimes")
    op.drop_table("runtimes")
    op.drop_column("goals", "execution_mode")
    op.drop_constraint("ck_managed_shadow_zero_escrow", "task_sessions", type_="check")
    op.drop_column("task_sessions", "execution_mode")
