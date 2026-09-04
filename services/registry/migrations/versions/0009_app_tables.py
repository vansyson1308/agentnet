"""app tables — DDL for the tables that previously existed only as ORM models

Revision ID: 0009_app_tables
Revises: 0008_society_phase2
Create Date: 2026-09-04

Creates audit_log, provisioning_providers, provisioning_services, projects,
scoped_tokens, project_resources, orchestrator_partners (registry ORM) and
approval_requests (payment ORM). Before this revision nothing created these
tables on a fresh database — they only appeared if ``Base.metadata.create_all``
was called, which no service does.

Idempotent: every statement is ``IF NOT EXISTS`` so a fresh database that was
bootstrapped from ``init-db/17-app-tables.sql`` and then stamped at 0003 can
run ``upgrade head`` without conflict. The DDL lives in
``app/schema_app_sql.py`` (single source of truth) and is embedded here via
import — the registry image copies ``app/`` next to ``migrations/`` and env.py
puts the service root on sys.path.
"""

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401

from app.schema_app_sql import APP_TABLES, APP_TABLES_SQL

revision = "0009_app_tables"
down_revision = "0008_society_phase2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(APP_TABLES_SQL)


def downgrade() -> None:
    for table in APP_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
