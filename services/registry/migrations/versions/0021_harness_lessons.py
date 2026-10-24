"""Society: the improvement harness (Harness² lessons for the coding harness)

Revision ID: 0021_harness_lessons
Revises: 0020_design_review
Create Date: 2026-10-24

Additive: ``society_harness_lessons`` (SOCIETY_HARNESS_SQL, the statement init-db/16 ends with). ADR-0011.
"""

import pathlib
import sys

from alembic import op

revision = "0021_harness_lessons"
down_revision = "0020_design_review"
branch_labels = None
depends_on = None

_APP = pathlib.Path(__file__).resolve().parents[2]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.society.schema_sql import SOCIETY_HARNESS_SQL  # noqa: E402


def upgrade() -> None:
    op.execute(SOCIETY_HARNESS_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS society_harness_lessons;")
