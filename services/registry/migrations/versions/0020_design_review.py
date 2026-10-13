"""Society: the design review (the company meets before a ticket is designed)

Revision ID: 0020_design_review
Revises: 0019_company_board
Create Date: 2026-10-13

Additive: ``society_design_reviews`` (SOCIETY_DESIGN_REVIEW_SQL, the statement init-db/16 ends with).
"""

import pathlib
import sys

from alembic import op

revision = "0020_design_review"
down_revision = "0019_company_board"
branch_labels = None
depends_on = None

_APP = pathlib.Path(__file__).resolve().parents[2]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.society.schema_sql import SOCIETY_DESIGN_REVIEW_SQL  # noqa: E402


def upgrade() -> None:
    op.execute(SOCIETY_DESIGN_REVIEW_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS society_design_reviews;")
