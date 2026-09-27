"""Autonomous Maintenance OS: incidents, repair cases, plans, activities,
artifacts, transitions, releases, known-good registry (ADR-0010)

Revision ID: 0014_maintenance_os
Revises: 0013_a2a_federation
Create Date: 2026-09-27

Additive only: new tables, indexes and immutability triggers. No existing
table or column is touched, so the migration can land (and roll back by a
Railway deployment rollback) with every maintenance flag OFF.

The DDL is embedded from its single source of truth so a fresh init-db bundle
and a migrated database converge on the same schema:

* ``app/maintenance/schema_sql.py`` (MAINTENANCE_SQL) -> init-db/19-maintenance-os.sql

``downgrade()`` drops exactly these tables. It exists for local development;
production is never downgraded automatically (ADR-0010 D19).
"""

import pathlib
import sys

from alembic import op

revision = "0014_maintenance_os"
down_revision = "0013_a2a_federation"
branch_labels = None
depends_on = None

_APP = pathlib.Path(__file__).resolve().parents[2]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.maintenance.schema_sql import MAINTENANCE_SQL, MAINTENANCE_TABLES  # noqa: E402


def upgrade() -> None:
    op.execute(MAINTENANCE_SQL)


def downgrade() -> None:
    for table in reversed(MAINTENANCE_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE;")
    op.execute("DROP FUNCTION IF EXISTS maintenance_immutable_row();")
