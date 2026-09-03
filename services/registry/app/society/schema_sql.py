"""Idempotent DDL for the Autonomous Society Runtime tables.

Single source of truth for the schema, consumed by BOTH:

* ``services/registry/migrations/versions/0007_society_runtime.py`` — the
  alembic migration that existing databases apply on container start, and
* ``services/registry/init-db/16-society-runtime.sql`` — the bootstrap
  bundle Postgres runs on a *fresh* volume (``tests/test_alembic.py`` and
  ``tests/society/test_schema_sync.py`` assert the two stay identical).

Every statement uses ``IF NOT EXISTS`` so running it twice — fresh DB via
init-db, then ``alembic upgrade head`` on top — is a no-op the second time.

Design notes (see docs/adr/0001-autonomous-society-runtime.md):

* ``society_events`` is append-oriented: rows are inserted once and only
  their ``status`` / ``dispatched_at`` / ``processed_at`` change.
* ``agent_runs`` carries the lease (``worker_id`` + ``lease_expires_at``)
  so a crashed worker's run is re-claimable after expiry. ``(agent_id,
  event_id)`` is UNIQUE — an event wakes a given agent at most once.
* ``agent_intents.idempotency_key`` is UNIQUE — re-executing a run after a
  crash cannot duplicate a side effect that was already recorded.
* ``agent_capability_grants`` is the ONLY place an agent's permissions
  live. No intent type can write to it (enforced in policy.py + tests).
* ``code_candidates`` is the durable record of the Builder → QA →
  Security engineering loop; PASS/FAIL lives here, not in chat text.
"""

SOCIETY_RUNTIME_SQL = r"""
-- ============================================================
-- Autonomous Society Runtime v1 (durable event / run / intent model)
-- ============================================================

-- spans.extra_data: the ORM (Span.extra_data) and every writer (task_service,
-- worker, society runtime) use extra_data, but 01-init.sql created the
-- column as "metadata" — fresh volumes could not persist spans through the
-- ORM at all. Rename when only the old name exists; otherwise add the column.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'spans' AND column_name = 'metadata')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'spans' AND column_name = 'extra_data') THEN
        ALTER TABLE spans RENAME COLUMN metadata TO extra_data;
    END IF;
END $$;
ALTER TABLE spans ADD COLUMN IF NOT EXISTS extra_data JSONB DEFAULT '{}'::jsonb;

-- agent_chat is the existing inter-agent messaging table the runtime
-- writes SEND_MESSAGE intents into. It had no DDL in init-db (the ORM model
-- existed but nothing created it on fresh volumes); ensure it here.
CREATE TABLE IF NOT EXISTS agent_chat (
    id               UUID PRIMARY KEY,
    from_agent_id    UUID NOT NULL REFERENCES agents(id),
    to_agent_id      UUID REFERENCES agents(id),
    message_type     VARCHAR(32) NOT NULL,
    title            VARCHAR NOT NULL,
    content          TEXT NOT NULL,
    msg_metadata     JSON DEFAULT '{}'::json,
    thread_id        UUID NOT NULL,
    is_read          BOOLEAN DEFAULT FALSE,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_chat_to_created ON agent_chat (to_agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_chat_thread ON agent_chat (thread_id);

CREATE TABLE IF NOT EXISTS society_events (
    id               UUID PRIMARY KEY,
    event_type       VARCHAR(128) NOT NULL,
    actor_type       VARCHAR(32)  NOT NULL DEFAULT 'system',
    actor_id         UUID,
    subject_type     VARCHAR(64),
    subject_id       UUID,
    payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
    correlation_id   UUID NOT NULL,
    causation_id     UUID REFERENCES society_events(id) ON DELETE SET NULL,
    causation_depth  INTEGER NOT NULL DEFAULT 0,
    idempotency_key  VARCHAR(160) UNIQUE,
    status           VARCHAR(32) NOT NULL DEFAULT 'pending',
    dispatch_note    TEXT,
    trace_id         UUID,
    source_run_id    UUID,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dispatched_at    TIMESTAMPTZ,
    processed_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_society_events_status_created ON society_events (status, created_at);
CREATE INDEX IF NOT EXISTS idx_society_events_correlation ON society_events (correlation_id);
CREATE INDEX IF NOT EXISTS idx_society_events_type_created ON society_events (event_type, created_at);

CREATE TABLE IF NOT EXISTS agent_runs (
    id                UUID PRIMARY KEY,
    agent_id          UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    event_id          UUID NOT NULL REFERENCES society_events(id) ON DELETE CASCADE,
    role              VARCHAR(64),
    status            VARCHAR(32) NOT NULL DEFAULT 'queued',
    worker_id         VARCHAR(128),
    lease_expires_at  TIMESTAMPTZ,
    attempt           INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL DEFAULT 3,
    not_before        TIMESTAMPTZ,
    started_at        TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ,
    model_provider    VARCHAR(64),
    model_name        VARCHAR(128),
    prompt_version    VARCHAR(32),
    context_digest    VARCHAR(64),
    context_summary   JSONB NOT NULL DEFAULT '{}'::jsonb,
    decision_summary  TEXT,
    intents_count     INTEGER NOT NULL DEFAULT 0,
    tokens_in         INTEGER,
    tokens_out        INTEGER,
    cost_usd          NUMERIC(12, 6) NOT NULL DEFAULT 0,
    error             TEXT,
    sleep_until       TIMESTAMPTZ,
    correlation_id    UUID NOT NULL,
    trace_id          UUID,
    span_id           UUID,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_agent_runs_agent_event UNIQUE (agent_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_claimable ON agent_runs (status, not_before, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent_created ON agent_runs (agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_correlation ON agent_runs (correlation_id);

CREATE TABLE IF NOT EXISTS agent_intents (
    id                UUID PRIMARY KEY,
    run_id            UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    agent_id          UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    seq               INTEGER NOT NULL,
    intent_type       VARCHAR(64) NOT NULL,
    payload           JSONB NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key   VARCHAR(160) NOT NULL UNIQUE,
    risk_class        VARCHAR(16) NOT NULL DEFAULT 'low',
    policy_decision   VARCHAR(32) NOT NULL DEFAULT 'pending',
    policy_reason     TEXT,
    execution_status  VARCHAR(32) NOT NULL DEFAULT 'pending',
    result            JSONB NOT NULL DEFAULT '{}'::jsonb,
    error             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    executed_at       TIMESTAMPTZ,
    CONSTRAINT uq_agent_intents_run_seq UNIQUE (run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_agent_intents_agent_created ON agent_intents (agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_intents_status ON agent_intents (execution_status);

CREATE TABLE IF NOT EXISTS agent_capability_grants (
    id                          UUID PRIMARY KEY,
    agent_id                    UUID NOT NULL UNIQUE REFERENCES agents(id) ON DELETE CASCADE,
    role                        VARCHAR(64) NOT NULL,
    allowed_intents             JSONB NOT NULL DEFAULT '[]'::jsonb,
    approval_required_intents   JSONB NOT NULL DEFAULT '[]'::jsonb,
    resource_scopes             JSONB NOT NULL DEFAULT '{}'::jsonb,
    risk_ceiling                VARCHAR(16) NOT NULL DEFAULT 'low',
    max_runs_per_hour           INTEGER NOT NULL DEFAULT 20,
    max_intents_per_run         INTEGER NOT NULL DEFAULT 5,
    daily_model_budget_usd      NUMERIC(12, 6) NOT NULL DEFAULT 1.0,
    max_task_escrow_credits     INTEGER NOT NULL DEFAULT 0,
    wake_cooldown_seconds       INTEGER NOT NULL DEFAULT 30,
    enabled                     BOOLEAN NOT NULL DEFAULT TRUE,
    paused_until                TIMESTAMPTZ,
    consecutive_failures        INTEGER NOT NULL DEFAULT 0,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS code_candidates (
    id                        UUID PRIMARY KEY,
    proposal_id               UUID REFERENCES improvement_proposals(id) ON DELETE SET NULL,
    task_id                   UUID REFERENCES task_sessions(id) ON DELETE SET NULL,
    goal_id                   UUID REFERENCES goals(id) ON DELETE SET NULL,
    correlation_id            UUID NOT NULL,
    requested_by_agent_id     UUID REFERENCES agents(id) ON DELETE SET NULL,
    builder_agent_id          UUID REFERENCES agents(id) ON DELETE SET NULL,
    builder_run_id            UUID REFERENCES agent_runs(id) ON DELETE SET NULL,
    qa_agent_id               UUID REFERENCES agents(id) ON DELETE SET NULL,
    qa_run_id                 UUID REFERENCES agent_runs(id) ON DELETE SET NULL,
    security_agent_id         UUID REFERENCES agents(id) ON DELETE SET NULL,
    security_run_id           UUID REFERENCES agent_runs(id) ON DELETE SET NULL,
    title                     VARCHAR(255) NOT NULL,
    spec                      JSONB NOT NULL DEFAULT '{}'::jsonb,
    branch_name               VARCHAR(255),
    workspace_path            VARCHAR(512),
    base_sha                  VARCHAR(64),
    head_sha                  VARCHAR(64),
    diff_stat                 TEXT,
    patch_summary             TEXT,
    changed_files             JSONB NOT NULL DEFAULT '[]'::jsonb,
    status                    VARCHAR(32) NOT NULL DEFAULT 'requested',
    qa_report                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    security_report           JSONB NOT NULL DEFAULT '{}'::jsonb,
    requires_security_review  BOOLEAN NOT NULL DEFAULT FALSE,
    error                     TEXT,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_code_candidates_status ON code_candidates (status, created_at);
CREATE INDEX IF NOT EXISTS idx_code_candidates_correlation ON code_candidates (correlation_id);
"""


# Phase 2 additions (migration 0008): operator role, durable approvals with
# resume leases, model-request accounting, ingress rate-limit index.
SOCIETY_PHASE2_SQL = r"""
-- ============================================================
-- Autonomous Society Runtime v1 — Phase 2 (operators, approvals, model accounting)
-- ============================================================

-- Durable operator / service-identity role. NULL = ordinary user.
ALTER TABLE users ADD COLUMN IF NOT EXISTS society_role VARCHAR(32);

-- Model-request accounting (requests/retries/timeouts are distinct from run attempts).
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS model_requests INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS model_retries INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS model_timeouts INTEGER NOT NULL DEFAULT 0;

-- Resume lease for approved intents (claimed by exactly one worker).
ALTER TABLE agent_intents ADD COLUMN IF NOT EXISTS resume_worker_id VARCHAR(128);
ALTER TABLE agent_intents ADD COLUMN IF NOT EXISTS resume_lease_expires_at TIMESTAMPTZ;
ALTER TABLE agent_intents ADD COLUMN IF NOT EXISTS resume_attempt INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_agent_intents_resumable ON agent_intents (execution_status, resume_lease_expires_at);

-- One durable decision per intent; who/what/when/why and the terminal outcome.
CREATE TABLE IF NOT EXISTS intent_approvals (
    id                       UUID PRIMARY KEY,
    intent_id                UUID NOT NULL UNIQUE REFERENCES agent_intents(id) ON DELETE CASCADE,
    run_id                   UUID NOT NULL,
    agent_id                 UUID NOT NULL,
    decided_by_user_id       UUID REFERENCES users(id) ON DELETE SET NULL,
    decision                 VARCHAR(16) NOT NULL,
    reason                   TEXT,
    original_policy_reason   TEXT,
    decided_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resumed_at               TIMESTAMPTZ,
    executed_at              TIMESTAMPTZ,
    final_state              VARCHAR(32),
    resume_error             TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_intent_approvals_decision ON intent_approvals (decision, decided_at);

-- Ingress rate limiting counts events per actor per window.
CREATE INDEX IF NOT EXISTS idx_society_events_actor_created ON society_events (actor_type, actor_id, created_at);
"""

SOCIETY_RUNTIME_SQL = SOCIETY_RUNTIME_SQL + SOCIETY_PHASE2_SQL
