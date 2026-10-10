"""Society: company charter -- objective status and tickets

Revision ID: 0017_company
Revises: 0016_bench_reports
Create Date: 2026-10-10

Additive: ``society_objective_status``, ``society_tickets`` (SOCIETY_COMPANY_SQL, the statement init-db/16 ends with).
"""

import pathlib
import sys

from alembic import op

revision = "0017_company"
down_revision = "0016_bench_reports"
branch_labels = None
depends_on = None

_APP = pathlib.Path(__file__).resolve().parents[2]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.society.schema_sql import SOCIETY_COMPANY_SQL  # noqa: E402


def upgrade() -> None:
    op.execute(SOCIETY_COMPANY_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS society_tickets, society_objective_status;")
