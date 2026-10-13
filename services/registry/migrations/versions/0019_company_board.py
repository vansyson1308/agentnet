"""Society: the board's standing delegation for the daily plan

Revision ID: 0019_company_board
Revises: 0018_daily_plans
Create Date: 2026-10-13

Additive: ``society_daily_plans.decided_by`` / ``board_note`` (SOCIETY_COMPANY_BOARD_SQL, the statement init-db/16 ends with).
"""

import pathlib
import sys

from alembic import op

revision = "0019_company_board"
down_revision = "0018_daily_plans"
branch_labels = None
depends_on = None

_APP = pathlib.Path(__file__).resolve().parents[2]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.society.schema_sql import SOCIETY_COMPANY_BOARD_SQL  # noqa: E402


def upgrade() -> None:
    op.execute(SOCIETY_COMPANY_BOARD_SQL)


def downgrade() -> None:
    op.execute("ALTER TABLE society_daily_plans DROP COLUMN IF EXISTS board_note; ALTER TABLE society_daily_plans DROP COLUMN IF EXISTS decided_by;")
