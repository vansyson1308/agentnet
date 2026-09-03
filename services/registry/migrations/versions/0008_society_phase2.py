"""society phase 2 — operator role, durable intent approvals + resume leases, model accounting

Revision ID: 0008_society_phase2
Revises: 0007_society_runtime
Create Date: 2026-09-03

Idempotent (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS); the same statements are
part of init-db/16-society-runtime.sql for fresh volumes.
"""

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401

from app.society.schema_sql import SOCIETY_PHASE2_SQL

revision = "0008_society_phase2"
down_revision = "0007_society_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(SOCIETY_PHASE2_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS intent_approvals CASCADE")
    op.execute("DROP INDEX IF EXISTS idx_agent_intents_resumable")
    op.execute("DROP INDEX IF EXISTS idx_society_events_actor_created")
    for col in ("resume_worker_id", "resume_lease_expires_at", "resume_attempt"):
        op.execute(f"ALTER TABLE agent_intents DROP COLUMN IF EXISTS {col}")
    for col in ("model_requests", "model_retries", "model_timeouts"):
        op.execute(f"ALTER TABLE agent_runs DROP COLUMN IF EXISTS {col}")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS society_role")
