"""society runtime — durable events, agent runs, typed intents, grants, code candidates

Revision ID: 0007_society_runtime
Revises: 0006_email_verified
Create Date: 2026-09-03

Idempotent: every statement is ``IF NOT EXISTS`` so a fresh database that
was bootstrapped from ``init-db/16-society-runtime.sql`` and then stamped
at 0003 can run ``upgrade head`` without conflict. The DDL lives in
``app/society/schema_sql.py`` (single source of truth) and is embedded
here via import — the registry image copies ``app/`` next to
``migrations/`` and env.py already puts the service root on sys.path.
"""

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401

from app.society.schema_sql import SOCIETY_RUNTIME_SQL

revision = "0007_society_runtime"
down_revision = "0006_email_verified"
branch_labels = None
depends_on = None

_TABLES = (
    "code_candidates",
    "agent_capability_grants",
    "agent_intents",
    "agent_runs",
    "society_events",
)


def upgrade() -> None:
    op.execute(SOCIETY_RUNTIME_SQL)


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
