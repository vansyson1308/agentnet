-- ============================================================
-- Agent Social Graph Migration
-- ============================================================
-- Tracks agent-to-agent interaction patterns.
-- Uses PostgreSQL adjacency table (not Neo4j) for simplicity.
-- Sufficient for <100,000 agents.
--
-- Invariant: Social graph is derived/read-only data.
-- Does NOT affect wallet balances or escrow state.
-- ============================================================

-- Interaction types
CREATE TYPE interaction_type AS ENUM (
  'task_completed',    -- A completed a task for B (or vice versa)
  'task_failed',       -- A task between A and B failed
  'offer_accepted',    -- A's offer to B was accepted
  'offer_rejected',    -- A's offer to B was rejected
  'referral'           -- A referred B
);

-- Agent interactions table (adjacency list)
CREATE TABLE IF NOT EXISTS agent_interactions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  from_agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  to_agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  interaction_type interaction_type NOT NULL,
  count INT NOT NULL DEFAULT 1,
  total_volume BIGINT NOT NULL DEFAULT 0,  -- Total credits exchanged
  last_interaction_at TIMESTAMP NOT NULL DEFAULT NOW(),
  first_interaction_at TIMESTAMP NOT NULL DEFAULT NOW(),
  UNIQUE(from_agent_id, to_agent_id, interaction_type)
);

-- Indexes for graph queries
CREATE INDEX IF NOT EXISTS idx_interactions_from ON agent_interactions(from_agent_id);
CREATE INDEX IF NOT EXISTS idx_interactions_to ON agent_interactions(to_agent_id);
CREATE INDEX IF NOT EXISTS idx_interactions_type ON agent_interactions(interaction_type);
CREATE INDEX IF NOT EXISTS idx_interactions_count ON agent_interactions(count DESC);

-- Materialized view for agent connection strength
-- Refreshed periodically by the worker
CREATE MATERIALIZED VIEW IF NOT EXISTS agent_connection_strength AS
SELECT
  from_agent_id,
  to_agent_id,
  SUM(count) AS total_interactions,
  SUM(total_volume) AS total_volume,
  MAX(last_interaction_at) AS last_interaction,
  COUNT(DISTINCT interaction_type) AS interaction_diversity
FROM agent_interactions
GROUP BY from_agent_id, to_agent_id
ORDER BY total_interactions DESC;

CREATE UNIQUE INDEX IF NOT EXISTS idx_connection_strength_agents
  ON agent_connection_strength(from_agent_id, to_agent_id);
