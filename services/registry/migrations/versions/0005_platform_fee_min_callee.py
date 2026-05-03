"""ensure callee always gets >=1 unit after platform fee

Revision ID: 0005_platform_fee_min_callee
Revises: 0004_balance_checks
Create Date: 2026-05-03

The trigger in ``init-db/02-platform-fee.sql`` previously charged
``GREATEST(1, FLOOR(amount * rate))`` as the platform fee, which for
``amount=1`` left the callee with 0. This migration replaces the
trigger function to cap the fee at ``amount - 1`` and skip the fee
entirely for ``amount=1`` so callees never work for free.
"""

from alembic import op

revision = "0005_platform_fee_min_callee"
down_revision = "0004_balance_checks"
branch_labels = None
depends_on = None


_NEW_FN = """
CREATE OR REPLACE FUNCTION update_wallet_balances()
RETURNS TRIGGER AS $$
DECLARE
    fee_amount BIGINT;
    net_amount BIGINT;
    platform_wallet_id UUID := '00000000-0000-0000-0000-000000000001';
BEGIN
    IF NEW.status = 'completed' AND OLD.status != 'completed' THEN
        IF NEW.type = 'payment' AND NEW.platform_fee_rate > 0 AND NEW.amount > 1 THEN
            fee_amount := LEAST(
                NEW.amount - 1,
                GREATEST(1, FLOOR(NEW.amount * NEW.platform_fee_rate))
            );
            net_amount := NEW.amount - fee_amount;
            NEW.platform_fee := fee_amount;
        ELSE
            fee_amount := 0;
            net_amount := NEW.amount;
            NEW.platform_fee := 0;
        END IF;

        IF NEW.from_wallet IS NOT NULL THEN
            IF NEW.currency = 'credits' THEN
                UPDATE wallets
                SET balance_credits = balance_credits - NEW.amount,
                    updated_at = NOW()
                WHERE id = NEW.from_wallet;
            ELSE
                UPDATE wallets
                SET balance_usdc = balance_usdc - NEW.amount,
                    updated_at = NOW()
                WHERE id = NEW.from_wallet;
            END IF;
        END IF;

        IF NEW.to_wallet IS NOT NULL THEN
            IF NEW.currency = 'credits' THEN
                UPDATE wallets
                SET balance_credits = balance_credits + net_amount,
                    updated_at = NOW()
                WHERE id = NEW.to_wallet;
            ELSE
                UPDATE wallets
                SET balance_usdc = balance_usdc + net_amount,
                    updated_at = NOW()
                WHERE id = NEW.to_wallet;
            END IF;
        END IF;

        IF fee_amount > 0 THEN
            IF NEW.currency = 'credits' THEN
                UPDATE wallets
                SET balance_credits = balance_credits + fee_amount,
                    updated_at = NOW()
                WHERE id = platform_wallet_id;
            ELSE
                UPDATE wallets
                SET balance_usdc = balance_usdc + fee_amount,
                    updated_at = NOW()
                WHERE id = platform_wallet_id;
            END IF;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE 'plpgsql';
"""

_OLD_FN = """
CREATE OR REPLACE FUNCTION update_wallet_balances()
RETURNS TRIGGER AS $$
DECLARE
    fee_amount BIGINT;
    net_amount BIGINT;
    platform_wallet_id UUID := '00000000-0000-0000-0000-000000000001';
BEGIN
    IF NEW.status = 'completed' AND OLD.status != 'completed' THEN
        IF NEW.type = 'payment' AND NEW.platform_fee_rate > 0 THEN
            fee_amount := GREATEST(1, FLOOR(NEW.amount * NEW.platform_fee_rate));
            net_amount := NEW.amount - fee_amount;
            NEW.platform_fee := fee_amount;
        ELSE
            fee_amount := 0;
            net_amount := NEW.amount;
        END IF;
        IF NEW.from_wallet IS NOT NULL THEN
            IF NEW.currency = 'credits' THEN
                UPDATE wallets SET balance_credits = balance_credits - NEW.amount, updated_at = NOW() WHERE id = NEW.from_wallet;
            ELSE
                UPDATE wallets SET balance_usdc = balance_usdc - NEW.amount, updated_at = NOW() WHERE id = NEW.from_wallet;
            END IF;
        END IF;
        IF NEW.to_wallet IS NOT NULL THEN
            IF NEW.currency = 'credits' THEN
                UPDATE wallets SET balance_credits = balance_credits + net_amount, updated_at = NOW() WHERE id = NEW.to_wallet;
            ELSE
                UPDATE wallets SET balance_usdc = balance_usdc + net_amount, updated_at = NOW() WHERE id = NEW.to_wallet;
            END IF;
        END IF;
        IF fee_amount > 0 THEN
            IF NEW.currency = 'credits' THEN
                UPDATE wallets SET balance_credits = balance_credits + fee_amount, updated_at = NOW() WHERE id = platform_wallet_id;
            ELSE
                UPDATE wallets SET balance_usdc = balance_usdc + fee_amount, updated_at = NOW() WHERE id = platform_wallet_id;
            END IF;
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
