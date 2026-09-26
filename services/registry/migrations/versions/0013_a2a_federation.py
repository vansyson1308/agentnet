"""A2A v1 federation + autonomous-company cadence tables (ADR-0009)

Revision ID: 0013_a2a_federation
Revises: 0012_memory_validation_history
Create Date: 2026-09-25

Additive only: new tables, indexes and one append-only trigger. No existing
table or column is touched, so the migration can land (and roll back by
Railway deployment rollback) with every A2A feature flag OFF.

The DDL is embedded from the single sources of truth so a fresh init-db
bundle and a migrated database converge on the same schema:

* ``app/a2a/schema_sql.py``            (A2A_SQL)  -> init-db/18-a2a-federation.sql
* ``app/society/schema_sql.py``        (SOCIETY_PHASE8_SQL, part of 16-society-runtime.sql)

``downgrade()`` drops exactly these tables. It exists for local development;
production is never downgraded automatically (ADR-0009 D17).
"""

import pathlib
import sys

from alembic import op

revision = "0013_a2a_federation"
down_revision = "0012_memory_validation_history"
branch_labels = None
depends_on = None

_APP = pathlib.Path(__file__).resolve().parents[2]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.a2a.schema_sql import A2A_SQL, A2A_TABLES  # noqa: E402
from app.society.schema_sql import SOCIETY_PHASE8_SQL  # noqa: E402

PHASE8_SOCIETY_TABLES = ("society_company_cycles", "society_incident_freezes")


def upgrade() -> None:
    op.execute(A2A_SQL)
    op.execute(SOCIETY_PHASE8_SQL)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_a2a_audit_log_append_only ON a2a_audit_log;")
    op.execute("DROP FUNCTION IF EXISTS a2a_audit_log_append_only();")
    for table in reversed(A2A_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE;")
    for table in PHASE8_SOCIETY_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE;")
