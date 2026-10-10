"""Society: builder bench reports (the self-improvement loop's scoreboard)

Revision ID: 0016_bench_reports
Revises: 0015_activity_turn_log
Create Date: 2026-10-09

Additive: ``society_bench_reports`` (SOCIETY_BENCH_SQL, the statement init-db/16 ends with).
"""

import pathlib
import sys

from alembic import op

revision = "0016_bench_reports"
down_revision = "0015_activity_turn_log"
branch_labels = None
depends_on = None

_APP = pathlib.Path(__file__).resolve().parents[2]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.society.schema_sql import SOCIETY_BENCH_SQL  # noqa: E402


def upgrade() -> None:
    op.execute(SOCIETY_BENCH_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS society_bench_reports;")
