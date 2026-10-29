"""Society: the owner's directive (Owner Console "Kim chi nam")

Revision ID: 0026_owner_directives
Revises: 0025_telegram_links
Create Date: 2026-10-29

Additive: ``society_owner_directives`` (SOCIETY_OWNER_DIRECTIVES_SQL): the owner's current direction,
append-only, one active row, written only by the operator API.
"""

import pathlib
import sys

from alembic import op

revision = "0026_owner_directives"
down_revision = "0025_telegram_links"
branch_labels = None
depends_on = None

_APP = pathlib.Path(__file__).resolve().parents[2]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.society.schema_sql import SOCIETY_OWNER_DIRECTIVES_SQL  # noqa: E402


def upgrade() -> None:
    op.execute(SOCIETY_OWNER_DIRECTIVES_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS society_owner_directives;")
    op.execute("DROP FUNCTION IF EXISTS society_owner_directives_append_only();")
