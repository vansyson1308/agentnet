"""society phase 3 — self-development: promotion records, change experiments, deployment
requests, memory provenance, trusted risk tier, model routing accounting

Revision ID: 0010_self_development
Revises: 0009_app_tables
Create Date: 2026-09-18

Idempotent (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS); the same statements are
part of init-db/16-society-runtime.sql for fresh volumes (generated from
app/society/schema_sql.py — edit THAT file).
"""

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401

from app.society.schema_sql import SOCIETY_PHASE3_SQL

revision = "0010_self_development"
down_revision = "0009_app_tables"
branch_labels = None
depends_on = None

PHASE3_TABLES = ("deployment_requests", "change_experiments", "code_promotions")


def upgrade() -> None:
    op.execute(SOCIETY_PHASE3_SQL)


def downgrade() -> None:
    for table in PHASE3_TABLES:  # children first (FKs)
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP INDEX IF EXISTS idx_code_candidates_diff_hash")
    op.execute("DROP INDEX IF EXISTS idx_memory_items_correlation")
    for col in ("risk_tier", "diff_hash", "diff_lines", "engineering_turns", "repo_reads"):
        op.execute(f"ALTER TABLE code_candidates DROP COLUMN IF EXISTS {col}")
    for col in ("model_tier", "route_reason", "tokens_cached", "output_format", "format_fallbacks"):
        op.execute(f"ALTER TABLE agent_runs DROP COLUMN IF EXISTS {col}")
    for col in ("source_type", "source_id", "correlation_id", "author_agent_id", "confidence", "validation_state", "expires_at", "superseded_by"):
        op.execute(f"ALTER TABLE memory_items DROP COLUMN IF EXISTS {col}")
