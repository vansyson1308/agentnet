"""Society: company daily plan (owner approval)

Revision ID: 0018_daily_plans
Revises: 0017_company
Create Date: 2026-10-10

Additive: ``society_daily_plans`` (SOCIETY_COMPANY_PLAN_SQL, the statement init-db/16 ends with).
"""

import pathlib
import sys

from alembic import op

revision = "0018_daily_plans"
down_revision = "0017_company"
branch_labels = None
depends_on = None

_APP = pathlib.Path(__file__).resolve().parents[2]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.society.schema_sql import SOCIETY_COMPANY_PLAN_SQL  # noqa: E402


def upgrade() -> None:
    op.execute(SOCIETY_COMPANY_PLAN_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS society_daily_plans;")
