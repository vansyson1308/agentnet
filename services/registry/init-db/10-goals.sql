-- 10-goals.sql
-- Adds the Goal entity + agents.current_goal_id / agents.mission so every
-- agent can carry a primary mission and 0..N active goals with priority,
-- success criteria, and a parent/child tree.
--
-- This migration is purely additive — no existing column dropped, no
-- wallet/escrow path touched. Safe to apply on running prod via:
--   psql $DATABASE_URL -f 10-goals.sql

CREATE TABLE IF NOT EXISTS goals (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title            TEXT NOT NULL,
    description      TEXT,
    owner_type       TEXT NOT NULL
                       CHECK (owner_type IN ('USER', 'AGENT', 'SOCIETY')),
    owner_id         UUID NOT NULL,
    priority         TEXT NOT NULL DEFAULT 'medium'
                       CHECK (priority IN ('low', 'medium', 'high', 'critical')),
    status           TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'paused', 'completed', 'failed', 'cancelled')),
    success_criteria JSONB NOT NULL DEFAULT '[]',
    parent_goal_id   UUID REFERENCES goals(id) ON DELETE SET NULL,
    target_date      TIMESTAMPTZ,
    completed_at     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_goals_owner   ON goals (owner_type, owner_id);
CREATE INDEX IF NOT EXISTS idx_goals_status  ON goals (status);
CREATE INDEX IF NOT EXISTS idx_goals_parent  ON goals (parent_goal_id);
CREATE INDEX IF NOT EXISTS idx_goals_created ON goals (created_at DESC);

-- Touch updated_at on every UPDATE.
CREATE OR REPLACE FUNCTION goals_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_goals_updated_at ON goals;
CREATE TRIGGER trg_goals_updated_at
    BEFORE UPDATE ON goals
    FOR EACH ROW EXECUTE FUNCTION goals_set_updated_at();

-- Wire goals to agents: a primary mission string + a pointer to the
-- agent's current Goal. ON DELETE SET NULL so deleting a goal does not
-- cascade-delete the agent.
ALTER TABLE agents
    ADD COLUMN IF NOT EXISTS mission         TEXT,
    ADD COLUMN IF NOT EXISTS current_goal_id UUID REFERENCES goals(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_agents_current_goal ON agents (current_goal_id);
