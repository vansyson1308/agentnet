"""Society: the north star's external revenue ledger

Revision ID: 0022_revenue_ledger
Revises: 0021_harness_lessons
Create Date: 2026-10-25

Additive: ``society_revenue_entries`` (SOCIETY_REVENUE_SQL, the statement init-db/16 ends with).
Record-only income an operator recorded outside escrow; it never touches wallets (docs/NORTH_STAR.md).
"""

import pathlib
import sys

from alembic import op

revision = "0022_revenue_ledger"
down_revision = "0021_harness_lessons"
branch_labels = None
depends_on = None

_APP = pathlib.Path(__file__).resolve().parents[2]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.society.schema_sql import SOCIETY_REVENUE_SQL  # noqa: E402


def upgrade() -> None:
    op.execute(SOCIETY_REVENUE_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS society_revenue_entries;")
