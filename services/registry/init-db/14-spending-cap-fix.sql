-- ============================================================
-- Spending Cap Fix — count reserved + daily_spent
-- ============================================================
-- The original check_spending_cap trigger (01-init.sql) only compares
-- daily_spending.amount + NEW.amount > spending_cap. daily_spending is
-- only updated when a transaction COMPLETES, so two concurrent task
-- creations both see the same baseline and can both reserve up to the
-- cap, blowing through it.
--
-- Application code (task_service.create_task_with_escrow) already takes
-- SELECT FOR UPDATE on the caller wallet and checks
--     daily_spent + reserved_credits + price > spending_cap
-- before reserving. This trigger is the second defense at the DB level
-- so direct SQL inserts and replication side-effects also enforce the
-- right invariant.
--
-- Money invariant: cap counts (already-spent + currently-reserved); a
-- new task can only enter "reserved" if it still fits.
-- ============================================================

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
      FOR SHARE;  -- prevent concurrent UPDATE while we evaluate

    SELECT COALESCE(amount, 0)
      INTO spent_today
      FROM daily_spending
      WHERE wallet_id = NEW.from_wallet AND date = today;

    -- Reserved already includes amounts for in-flight pending transactions
    -- (see task_service.create_task_with_escrow which bumps reserved_credits
    -- before inserting the transaction). To avoid double-counting NEW.amount
    -- (which the app has already added to reserved_credits) we subtract it
    -- back out here. spent_today is only completed transactions, no overlap.
    IF spent_today + (reserved_now - NEW.amount) + NEW.amount > cap THEN
        RAISE EXCEPTION 'Spending cap exceeded for wallet %: spent=%, reserved=%, requested=%, cap=%',
            NEW.from_wallet, spent_today, reserved_now, NEW.amount, cap;
    END IF;

    RETURN NEW;
END;
$$ language 'plpgsql';

-- Trigger already exists from 01-init.sql; the CREATE OR REPLACE above
-- swaps the function body in place.
