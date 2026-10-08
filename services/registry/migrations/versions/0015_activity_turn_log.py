"""Maintenance OS: structural per-turn activity telemetry

Revision ID: 0015_activity_turn_log
Revises: 0014_maintenance_os
Create Date: 2026-10-08

Additive: ``repair_activities.turn_log`` (JSONB, default ``[]``) holds, per model
turn of an activity try, the action/tool name, result bytes and token counts --
never model or repository text. It shows where an AuthorPatch try spends its
turns. The DDL is the same idempotent statement init-db/19 ends with
(``app/maintenance/schema_sql.py``).
"""

import pathlib
import sys

from alembic import op

revision = "0015_activity_turn_log"
down_revision = "0014_maintenance_os"
branch_labels = None
depends_on = None

_APP = pathlib.Path(__file__).resolve().parents[2]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.maintenance.schema_sql import MAINTENANCE_TURN_LOG_SQL  # noqa: E402


def upgrade() -> None:
    op.execute(MAINTENANCE_TURN_LOG_SQL)


def downgrade() -> None:
    op.execute("ALTER TABLE repair_activities DROP COLUMN IF EXISTS turn_log;")
