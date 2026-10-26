"""Society: more than one scheduled company cycle a day

Revision ID: 0023_company_slots
Revises: 0022_revenue_ledger
Create Date: 2026-10-26

Additive: ``society_company_cycles.slot`` and the (date, slot) unique index for scheduled cycles
(SOCIETY_COMPANY_SLOTS_SQL, SOCIETY_COMPANY_CYCLES_PER_DAY).
"""

import pathlib
import sys

from alembic import op

revision = "0023_company_slots"
down_revision = "0022_revenue_ledger"
branch_labels = None
depends_on = None

_APP = pathlib.Path(__file__).resolve().parents[2]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.society.schema_sql import SOCIETY_COMPANY_SLOTS_SQL  # noqa: E402


def upgrade() -> None:
    op.execute(SOCIETY_COMPANY_SLOTS_SQL)


def downgrade() -> None:
    # extra slots of a day keep their record as operator cycles (the one-per-day index comes back)
    op.execute("UPDATE society_company_cycles SET trigger = 'operator' WHERE trigger = 'scheduled' AND slot > 0;")
    op.execute("DROP INDEX IF EXISTS uq_society_company_cycles_scheduled_slot;")
    op.execute("ALTER TABLE society_company_cycles DROP COLUMN IF EXISTS slot;")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_society_company_cycles_scheduled_day ON society_company_cycles (cycle_date) WHERE trigger = 'scheduled';")
