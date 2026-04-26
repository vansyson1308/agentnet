-- 12-memory.sql
-- Adds the MemoryItem (Lesson) entity. Society-scope or agent-scope
-- durable lessons. Each completed/failed task can write one. Future
-- tasks read tagged memory before starting, breaking the
-- "agents repeat the same mistake" pattern.
--
-- Memory complements (does NOT duplicate) the spans/traces table:
-- spans = mechanical event log; memory = curated, human/agent-written
-- lessons with importance + tags.

CREATE TABLE IF NOT EXISTS memory_items (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- agent_id NULL means SOCIETY scope (visible to all agents)
    agent_id       UUID REFERENCES agents(id) ON DELETE CASCADE,
    scope          TEXT NOT NULL
                    CHECK (scope IN ('AGENT', 'SOCIETY')),
    title          TEXT NOT NULL,
    content        TEXT NOT NULL,
    tags           JSONB NOT NULL DEFAULT '[]',
    source_task_id UUID REFERENCES task_sessions(id) ON DELETE SET NULL,
    importance     INTEGER NOT NULL DEFAULT 50
                    CHECK (importance BETWEEN 0 AND 100),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_agent       ON memory_items (agent_id);
CREATE INDEX IF NOT EXISTS idx_memory_scope       ON memory_items (scope);
CREATE INDEX IF NOT EXISTS idx_memory_source_task ON memory_items (source_task_id);
CREATE INDEX IF NOT EXISTS idx_memory_created     ON memory_items (created_at DESC);
-- Tag containment search: WHERE tags @> '["billing"]'
CREATE INDEX IF NOT EXISTS idx_memory_tags        ON memory_items USING GIN (tags);

-- Sanity: agent-scope rows MUST have an agent_id; society-scope MUST NOT.
ALTER TABLE memory_items
    DROP CONSTRAINT IF EXISTS chk_memory_scope_agent_consistency;
ALTER TABLE memory_items
    ADD CONSTRAINT chk_memory_scope_agent_consistency
    CHECK (
        (scope = 'AGENT' AND agent_id IS NOT NULL) OR
        (scope = 'SOCIETY' AND agent_id IS NULL)
    );
