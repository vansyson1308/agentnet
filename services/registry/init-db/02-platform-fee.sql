-- ============================================================
-- Platform Fee Migration
-- ============================================================
-- Adds a platform fee mechanism to AgentNet escrow transactions.
--
-- Money invariant: caller_deduction = callee_credit + platform_fee
-- The trigger is atomic — fee is deducted in the same transaction.
-- ============================================================

-- Add platform fee columns to transactions table
ALTER TABLE transactions
  ADD COLUMN IF NOT EXISTS platform_fee BIGINT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS platform_fee_rate DECIMAL(5,4) DEFAULT 0.0250;
  -- Default 2.5% fee rate

-- Create platform wallet (owned by system)
-- This wallet collects all platform fees
INSERT INTO users (id, email, password_hash, kyc_status)
VALUES (
  '00000000-0000-0000-0000-000000000001',
  'platform@agentnet.io',
  '$2b$12$PLATFORM_SYSTEM_ACCOUNT_NOT_FOR_LOGIN',
  'verified'
)
ON CONFLICT (email) DO NOTHING;

INSERT INTO wallets (id, owner_type, owner_id, balance_credits, balance_usdc, spending_cap)
VALUES (
  '00000000-0000-0000-0000-000000000001',
  'user',
  '00000000-0000-0000-0000-000000000001',
  0, 0, 999999999
)
ON CONFLICT (id) DO NOTHING;

-- Update the wallet balance trigger to include platform fee
CREATE OR REPLACE FUNCTION update_wallet_balances()
RETURNS TRIGGER AS $$
DECLARE
    fee_amount BIGINT;
    net_amount BIGINT;
    platform_wallet_id UUID := '00000000-0000-0000-0000-000000000001';
BEGIN
    -- Only process completed transactions
    IF NEW.status = 'completed' AND OLD.status != 'completed' THEN
        -- Calculate platform fee (only for PAYMENT transactions)
        IF NEW.type = 'payment' AND NEW.platform_fee_rate > 0 THEN
            fee_amount := GREATEST(1, FLOOR(NEW.amount * NEW.platform_fee_rate));
            net_amount := NEW.amount - fee_amount;
            -- Store the computed fee on the transaction
            NEW.platform_fee := fee_amount;
        ELSE
            fee_amount := 0;
            net_amount := NEW.amount;
        END IF;

        -- Handle outgoing transaction (deduct full amount from caller)
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

        -- Handle incoming transaction (credit net amount to callee)
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

        -- Credit platform fee to platform wallet
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
$$ language 'plpgsql';

-- Recreate the trigger (DROP + CREATE to ensure it uses the new function)
DROP TRIGGER IF EXISTS update_wallet_balances_trigger ON transactions;
CREATE TRIGGER update_wallet_balances_trigger
    BEFORE UPDATE ON transactions
    FOR EACH ROW
    WHEN (NEW.status = 'completed' AND OLD.status != 'completed')
    EXECUTE FUNCTION update_wallet_balances();

-- Create index for platform fee reporting
CREATE INDEX IF NOT EXISTS idx_transactions_platform_fee
    ON transactions(platform_fee) WHERE platform_fee > 0;
