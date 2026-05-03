"""tighten check_spending_cap to count reserved + daily_spent

Revision ID: 0003_spending_cap_fix
Revises: 0002_idempotency_key
Create Date: 2026-05-03

Mirrors ``services/registry/init-db/14-spending-cap-fix.sql``.
"""

from alembic import op

revision = "0003_spending_cap_fix"
down_revision = "0002_idempotency_key"
branch_labels = None
depends_on = None


_NEW_FN = """
CREATE OR REPLACE FUNCTION check_spending_cap()
RETURNS TRIGGER AS $$
DECLARE
    today DATE := CURRENT_DATE;
    cap BIGINT;
    spent_today BIGINT;
    reserved_now BIGINT;
BEGIN
    IF NEW.from_wallet IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT spending_cap, COALESCE(reserved_credits, 0)
      INTO cap, reserved_now
      FROM wallets
      WHERE id = NEW.from_wallet
      FOR SHARE;

    SELECT COALESCE(amount, 0)
      INTO spent_today
      FROM daily_spending
      WHERE wallet_id = NEW.from_wallet AND date = today;

    IF spent_today + (reserved_now - NEW.amount) + NEW.amount > cap THEN
        RAISE EXCEPTION 'Spending cap exceeded for wallet %: spent=%, reserved=%, requested=%, cap=%',
            NEW.from_wallet, spent_today, reserved_now, NEW.amount, cap;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE 'plpgsql';
"""

_OLD_FN = """
CREATE OR REPLACE FUNCTION check_spending_cap()
RETURNS TRIGGER AS $$
DECLARE
    current_daily_spent BIGINT;
    wallet_spending_cap BIGINT;
    today DATE := CURRENT_DATE;
BEGIN
    IF NEW.from_wallet IS NOT NULL THEN
        SELECT spending_cap INTO wallet_spending_cap FROM wallets WHERE id = NEW.from_wallet;
        SELECT COALESCE(amount, 0) INTO current_daily_spent
          FROM daily_spending
          WHERE wallet_id = NEW.from_wallet AND date = today;
        IF current_daily_spent + NEW.amount > wallet_spending_cap THEN
            RAISE EXCEPTION 'Spending cap exceeded for wallet %', NEW.from_wallet;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE 'plpgsql';
"""


def upgrade() -> None:
    op.execute(_NEW_FN)


def downgrade() -> None:
    op.execute(_OLD_FN)
