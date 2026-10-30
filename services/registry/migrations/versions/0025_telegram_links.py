"""Society: Telegram boardroom operator links

Revision ID: 0025_telegram_links
Revises: 0024_owner_actions
Create Date: 2026-10-27

Additive: ``society_telegram_links`` (SOCIETY_TELEGRAM_LINKS_SQL): which Telegram account speaks for
which operator user (identity only; the role is re-checked on every command).
"""

import pathlib
import sys

from alembic import op

revision = "0025_telegram_links"
down_revision = "0024_owner_actions"
branch_labels = None
depends_on = None

_APP = pathlib.Path(__file__).resolve().parents[2]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.society.schema_sql import SOCIETY_TELEGRAM_LINKS_SQL  # noqa: E402


def upgrade() -> None:
    op.execute(SOCIETY_TELEGRAM_LINKS_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS society_telegram_links;")
