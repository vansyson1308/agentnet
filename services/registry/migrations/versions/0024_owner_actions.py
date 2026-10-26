"""Society: the owner action queue

Revision ID: 0024_owner_actions
Revises: 0023_company_slots
Create Date: 2026-10-26

Additive: ``society_owner_actions`` (SOCIETY_OWNER_ACTIONS_SQL): outside actions only the owner can take.
"""

import pathlib
import sys

from alembic import op

revision = "0024_owner_actions"
down_revision = "0023_company_slots"
branch_labels = None
depends_on = None

_APP = pathlib.Path(__file__).resolve().parents[2]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.society.schema_sql import SOCIETY_OWNER_ACTIONS_SQL  # noqa: E402


def upgrade() -> None:
    op.execute(SOCIETY_OWNER_ACTIONS_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS society_owner_actions;")
