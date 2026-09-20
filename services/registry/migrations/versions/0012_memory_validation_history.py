"""append-only audit history for memory validation state changes

Revision ID: 0012_memory_validation_history
Revises: 0011_expire_rehearsal_memory
Create Date: 2026-09-20

``memory_items.validation_state`` already existed and ``context.memory_rank``
already demoted ``refuted`` rows hard — but nothing could ever write that
value except the fitness engine, so a belief that turned out to be false had
no audited way to be corrected. Phase 5 hit exactly that wall: a memory
recording work the platform had refused could not be demoted without a
hand-written database edit, which the runbook forbids.

This adds the audit trail that makes correction legitimate: who changed a
memory's standing, when, why, and on what evidence. The DDL is embedded from
``app/society/schema_sql.py`` (SOCIETY_PHASE6_SQL) so a fresh init-db bundle
and a migrated database converge on the same schema.

The table is append-only at the DATABASE level, not by convention: a trigger
refuses UPDATE and DELETE from every caller, including the application. That
is the constitutional invariant "agents cannot alter audit history" expressed
where it cannot be argued with.

Nothing is deleted or rewritten by this migration; it only creates the table,
its index and its trigger. It is idempotent.
"""

import pathlib
import sys

from alembic import op

revision = "0012_memory_validation_history"
down_revision = "0011_expire_rehearsal_memory"
branch_labels = None
depends_on = None

_APP = pathlib.Path(__file__).resolve().parents[2]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.society.schema_sql import SOCIETY_PHASE6_SQL  # noqa: E402


def upgrade() -> None:
    op.execute(SOCIETY_PHASE6_SQL)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_memory_validation_events_append_only ON memory_validation_events;")
    op.execute("DROP FUNCTION IF EXISTS memory_validation_events_append_only();")
    op.execute("DROP TABLE IF EXISTS memory_validation_events;")
