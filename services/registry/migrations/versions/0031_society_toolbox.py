"""Society: the toolbox -- reusable tools agents design, and their measured trials

Revision ID: 0031_society_toolbox
Revises: 0030_transaction_amount_check
Create Date: 2026-11-02

``society_tools`` (immutable tool versions: workflow / prompt / checklist / toolkit) and
``society_tool_trials`` (one row per tool x completed run, shown or withheld) for
``app/society/toolbox.py`` (SOCIETY_TOOLBOX_SQL). Advisory data only; nothing here touches
wallets or transactions.
"""

import pathlib
import sys

from alembic import op

revision = "0031_society_toolbox"
down_revision = "0030_transaction_amount_check"
branch_labels = None
depends_on = None

_APP = pathlib.Path(__file__).resolve().parents[2]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.society.schema_sql import SOCIETY_TOOLBOX_SQL  # noqa: E402


def upgrade() -> None:
    op.execute(SOCIETY_TOOLBOX_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS society_tool_trials;")
    op.execute("DROP TABLE IF EXISTS society_tools;")
