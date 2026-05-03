-- ============================================================
-- Wallet balance non-negative CHECK constraints
-- ============================================================
-- Defense-in-depth. The application path goes through
-- services/registry/app/task_service.py which uses SELECT FOR UPDATE
-- and validates non-negative invariants in code. This adds a final
-- catch at the DB level so any future bug in the trigger or any
-- direct DB write that bypasses task_service still cannot push a
-- balance below zero.
--
-- Money invariant: Once these constraints are in place, a violation
-- is a fatal application bug — the transaction aborts and we surface
-- the error rather than silently corrupting the wallet.
-- ============================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_balance_credits_nonneg'
    ) THEN
        ALTER TABLE wallets
            ADD CONSTRAINT chk_balance_credits_nonneg CHECK (balance_credits >= 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_balance_usdc_nonneg'
    ) THEN
        ALTER TABLE wallets
            ADD CONSTRAINT chk_balance_usdc_nonneg CHECK (balance_usdc >= 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_reserved_credits_nonneg'
    ) THEN
        ALTER TABLE wallets
            ADD CONSTRAINT chk_reserved_credits_nonneg CHECK (reserved_credits >= 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_reserved_usdc_nonneg'
    ) THEN
        ALTER TABLE wallets
            ADD CONSTRAINT chk_reserved_usdc_nonneg CHECK (reserved_usdc >= 0);
    END IF;
END $$;
