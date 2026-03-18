-- ============================================================
-- Enhanced Reputation System Migration
-- ============================================================
-- Adds computed reputation fields to agents table.
-- These are derived from task_sessions and spans data.
--
-- Invariant: Reputation fields are read-only derived data.
-- They do NOT affect wallet balances or escrow state.
-- ============================================================

-- Add reputation columns to agents table
ALTER TABLE agents
  ADD COLUMN IF NOT EXISTS total_tasks_completed INT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS total_tasks_failed INT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS total_tasks_timeout INT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS success_rate FLOAT DEFAULT 0.0,
  ADD COLUMN IF NOT EXISTS avg_response_time_ms INT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS total_volume_credits BIGINT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS reputation_tier VARCHAR DEFAULT 'unranked',
  ADD COLUMN IF NOT EXISTS reputation_updated_at TIMESTAMP;

-- Create index for reputation-based queries
CREATE INDEX IF NOT EXISTS idx_agents_reputation_tier ON agents(reputation_tier);
CREATE INDEX IF NOT EXISTS idx_agents_success_rate ON agents(success_rate DESC);
CREATE INDEX IF NOT EXISTS idx_agents_verify_score ON agents(verify_score DESC);
