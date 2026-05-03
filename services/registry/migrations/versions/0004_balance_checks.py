"""wallet balance non-negative CHECK constraints

Revision ID: 0004_balance_checks
Revises: 0003_spending_cap_fix
Create Date: 2026-05-03

Mirrors ``services/registry/init-db/15-balance-checks.sql``.
"""

from alembic import op

revision = "0004_balance_checks"
down_revision = "0003_spending_cap_fix"
branch_labels = None
depends_on = None


_UP = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_balance_credits_nonneg') THEN
        ALTER TABLE wallets ADD CONSTRAINT chk_balance_credits_nonneg CHECK (balance_credits >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_balance_usdc_nonneg') THEN
        ALTER TABLE wallets ADD CONSTRAINT chk_balance_usdc_nonneg CHECK (balance_usdc >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_reserved_credits_nonneg') THEN
        ALTER TABLE wallets ADD CONSTRAINT chk_reserved_credits_nonneg CHECK (reserved_credits >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_reserved_usdc_nonneg') THEN
        ALTER TABLE wallets ADD CONSTRAINT chk_reserved_usdc_nonneg CHECK (reserved_usdc >= 0);
    END IF;
END $$;
"""

_DOWN = """
ALTER TABLE wallets DROP CONSTRAINT IF EXISTS chk_balance_credits_nonneg;
ALTER TABLE wallets DROP CONSTRAINT IF EXISTS chk_balance_usdc_nonneg;
ALTER TABLE wallets DROP CONSTRAINT IF EXISTS chk_reserved_credits_nonneg;
ALTER TABLE wallets DROP CONSTRAINT IF EXISTS chk_reserved_usdc_nonneg;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
