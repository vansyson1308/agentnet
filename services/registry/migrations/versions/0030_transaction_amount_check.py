"""transactions.amount non-negative CHECK (NOT VALID)

Revision ID: 0030_transaction_amount_check
Revises: 0029_ticket_sources
Create Date: 2026-10-30

Mirrors the last block of ``services/registry/init-db/15-balance-checks.sql``.
The balance trigger moves ``amount`` from payer to payee, so a negative amount
would pull money out of the payee. NOT VALID binds every new row and every
update without failing on historical rows.
"""

from alembic import op

revision = "0030_transaction_amount_check"
down_revision = "0029_ticket_sources"
branch_labels = None
depends_on = None


_UP = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_transaction_amount_nonneg') THEN
        ALTER TABLE transactions ADD CONSTRAINT chk_transaction_amount_nonneg CHECK (amount >= 0) NOT VALID;
    END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute("ALTER TABLE transactions DROP CONSTRAINT IF EXISTS chk_transaction_amount_nonneg;")
