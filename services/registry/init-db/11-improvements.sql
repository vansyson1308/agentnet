-- 11-improvements.sql
-- Adds the ImprovementProposal entity. When a task fails, a review
-- rejects, or QA flags an issue, the platform produces a structured
-- proposal: problem, root cause, proposed change, expected benefit,
-- risk, status. Proposals can be approved -> converted to a real Task,
-- then ultimately marked Implemented when that task ships.
--
-- This is the lifecycle backbone of the self-improvement loop.

CREATE TABLE IF NOT EXISTS improvement_proposals (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposed_by_agent_id UUID REFERENCES agents(id) ON DELETE SET NULL,
    proposed_by_user_id  UUID REFERENCES users(id)  ON DELETE SET NULL,
    source               TEXT NOT NULL
                           CHECK (source IN (
                               'self_reflection',
                               'task_failure',
                               'review_rejection',
                               'qa_failure',
                               'audit',
                               'human_feedback',
                               'runtime_error'
                           )),
    title                TEXT NOT NULL,
    problem              TEXT,
    root_cause           TEXT,
    proposed_change      TEXT,
    expected_benefit     TEXT,
    risk                 TEXT,
    status               TEXT NOT NULL DEFAULT 'PROPOSED'
                           CHECK (status IN (
                               'PROPOSED',
                               'UNDER_REVIEW',
                               'APPROVED',
                               'REJECTED',
                               'CONVERTED_TO_TASK',
                               'IMPLEMENTED'
                           )),
    target_scope         TEXT NOT NULL DEFAULT 'agent'
                           CHECK (target_scope IN ('agent', 'platform')),
    importance           INTEGER NOT NULL DEFAULT 50
                           CHECK (importance BETWEEN 0 AND 100),
    source_task_id       UUID REFERENCES task_sessions(id) ON DELETE SET NULL,
    converted_task_id    UUID REFERENCES task_sessions(id) ON DELETE SET NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_improvements_status      ON improvement_proposals (status);
CREATE INDEX IF NOT EXISTS idx_improvements_agent       ON improvement_proposals (proposed_by_agent_id);
CREATE INDEX IF NOT EXISTS idx_improvements_user        ON improvement_proposals (proposed_by_user_id);
CREATE INDEX IF NOT EXISTS idx_improvements_source_task ON improvement_proposals (source_task_id);
CREATE INDEX IF NOT EXISTS idx_improvements_created     ON improvement_proposals (created_at DESC);

-- The reflection loop relies on this index to skip already-proposed tasks
-- without a full scan. Partial index keeps it tiny.
CREATE INDEX IF NOT EXISTS idx_improvements_active_source_task
    ON improvement_proposals (source_task_id)
    WHERE source_task_id IS NOT NULL;

CREATE OR REPLACE FUNCTION improvements_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_improvements_updated_at ON improvement_proposals;
CREATE TRIGGER trg_improvements_updated_at
    BEFORE UPDATE ON improvement_proposals
    FOR EACH ROW EXECUTE FUNCTION improvements_set_updated_at();
