"""Society: the owner's conversation with the company (Owner Console)

Revision ID: 0027_owner_conversation
Revises: 0026_owner_directives
Create Date: 2026-10-29

Additive (SOCIETY_OWNER_THREAD_SQL): ``society_owner_thread`` (owner messages + the Governor's one
reply each, append-only), ``society_company_cycles.directive_id`` (one immediate cycle per directive)
and ``society_daily_plans.directive_id`` / ``owner_entry_id`` (where a plan came from).
"""

import pathlib
import sys

from alembic import op

revision = "0027_owner_conversation"
down_revision = "0026_owner_directives"
branch_labels = None
depends_on = None

_APP = pathlib.Path(__file__).resolve().parents[2]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.society.schema_sql import SOCIETY_OWNER_THREAD_SQL  # noqa: E402


def upgrade() -> None:
    op.execute(SOCIETY_OWNER_THREAD_SQL)


def downgrade() -> None:
    op.execute("ALTER TABLE society_daily_plans DROP COLUMN IF EXISTS owner_entry_id;")
    op.execute("ALTER TABLE society_daily_plans DROP COLUMN IF EXISTS directive_id;")
    op.execute("DROP INDEX IF EXISTS uq_society_company_cycles_directive;")
    op.execute("ALTER TABLE society_company_cycles DROP COLUMN IF EXISTS directive_id;")
    op.execute("DROP TABLE IF EXISTS society_owner_thread;")
    op.execute("DROP FUNCTION IF EXISTS society_owner_thread_append_only();")
