-- Reputation History Snapshot Table
-- Stores daily snapshots of agent reputation metrics.
-- Unique constraint on (agent_id, snapshot_date) enables upsert behavior.
CREATE TABLE IF NOT EXISTS agent_reputation_history (
    agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    snapshot_date DATE NOT NULL,
    reputation_tier TEXT NOT NULL,
    success_rate DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (agent_id, snapshot_date),
    UNIQUE (agent_id, snapshot_date)
);

-- Index for faster queries by agent_id and date descending
CREATE INDEX IF NOT EXISTS idx_reputation_history_agent_date
    ON agent_reputation_history (agent_id, snapshot_date DESC);