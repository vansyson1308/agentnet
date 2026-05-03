"""add transactions.idempotency_key + UNIQUE index

Revision ID: 0002_idempotency_key
Revises: 0001_baseline
Create Date: 2026-05-03

Mirrors ``services/registry/init-db/13-idempotency.sql`` so freshly
bootstrapped databases AND databases that ran the SQL bundle both
end up at the same schema after ``alembic upgrade head``.
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_idempotency_key"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Use IF NOT EXISTS so re-running over a DB already touched by
    # init-db/13-idempotency.sql is a no-op.
    op.execute(
        "ALTER TABLE transactions "
        "ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(64)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_idempotency_key "
        "ON transactions(idempotency_key) WHERE idempotency_key IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_transactions_idempotency_key")
    op.execute("ALTER TABLE transactions DROP COLUMN IF EXISTS idempotency_key")
