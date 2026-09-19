"""expire the canary-rehearsal memory that was written before rehearsal memory had a lifetime

Revision ID: 0011_expire_rehearsal_memory
Revises: 0010_self_development
Create Date: 2026-09-19

DATA remediation, not DDL: there is nothing here for init-db/ or
app/society/schema_sql.py, because a fresh volume has no residue.

Before this change a canary rehearsal left permanent memory rows behind. The
Scout then read its own residue back as prior experience — on Railway staging
every one of its five live memory rows was canary triage ("this signal was
non-actionable"), and it cited them when declining to act on later signals.
``executor._write_memory`` now gives rehearsal-written memory a lifetime; this
expires the rows written before it did.

Nothing is deleted: the rows keep their provenance and stay queryable for
audit. They simply stop being served as live memory (``context._memory``
filters on ``expires_at``). Only rows whose correlation was started by a
canary are touched; memory from real signals is never in scope.
"""

from alembic import op

revision = "0011_expire_rehearsal_memory"
down_revision = "0010_self_development"
branch_labels = None
depends_on = None

EXPIRE_REHEARSAL_MEMORY = """
UPDATE memory_items m
   SET expires_at = now()
 WHERE m.expires_at IS NULL
   AND m.correlation_id IS NOT NULL
   AND EXISTS (
         SELECT 1 FROM society_events e
          WHERE e.correlation_id = m.correlation_id
            AND e.idempotency_key LIKE 'canary-%'
       );
"""


def upgrade() -> None:
    op.execute(EXPIRE_REHEARSAL_MEMORY)


def downgrade() -> None:
    # Irreversible by design: which rows had no expiry before is not recorded,
    # and un-expiring rehearsal residue would restore the defect.
    pass
