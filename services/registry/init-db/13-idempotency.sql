-- ============================================================
-- Transaction Idempotency Migration
-- ============================================================
-- Adds an idempotency_key column to the transactions table so that
-- a retry of a task-creating request (e.g. network blip) produces a
-- single row instead of duplicating the escrow lock.
--
-- The application layer (services/registry/app/task_service.py) reads
-- the inbound 'Idempotency-Key' header (or WS field) and writes it
-- here. The UNIQUE constraint at the DB level guarantees that two
-- concurrent requests with the same key cannot both succeed.
--
-- Money invariant: still "balance only changes via DB trigger". This
-- migration just prevents creating two pending transactions for what
-- the client considered one logical operation.
-- ============================================================

ALTER TABLE transactions
  ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(64);

CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_idempotency_key
  ON transactions(idempotency_key)
  WHERE idempotency_key IS NOT NULL;
