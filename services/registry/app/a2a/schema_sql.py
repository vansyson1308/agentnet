"""Idempotent DDL for A2A v1 federation (ADR-0009 D5, D12, D18).

Single source of truth, consumed by BOTH:

* ``migrations/versions/0013_a2a_federation.py`` -- existing databases, and
* ``init-db/18-a2a-federation.sql`` -- a fresh volume's bootstrap bundle
  (``tests/test_a2a_schema.py`` asserts the file is byte-identical).

Everything is additive (new tables only) and ``IF NOT EXISTS``, so applying it
on top of the bundle, or twice, is a no-op. The A2A task state is protocol /
integration state: it deliberately lives here and NOT in ``task_sessions``,
whose economic state machine is unchanged (ADR-0009 D5).
"""

A2A_TABLES = (
    "a2a_tasks",
    "a2a_messages",
    "a2a_artifacts",
    "a2a_task_events",
    "a2a_audit_log",
    "a2a_remote_agents",
    "a2a_remote_card_versions",
    "a2a_connections",
    "a2a_outbound_calls",
)

A2A_TASK_STATES = (
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_AUTH_REQUIRED",
)

REMOTE_AGENT_STATES = ("discovered", "verified", "degraded", "quarantined", "blocked")

A2A_SQL = r"""
-- ============================================================
-- A2A v1 federation (ADR-0009). Generated from
-- services/registry/app/a2a/schema_sql.py -- edit THAT file.
-- ============================================================

-- A protocol task. Integration state only: an economic TaskSession is linked
-- (task_session_id) once real, escrow-backed execution begins.
CREATE TABLE IF NOT EXISTS a2a_tasks (
    id                UUID PRIMARY KEY,
    context_id        UUID NOT NULL,
    tenant_agent_id   UUID REFERENCES agents(id) ON DELETE SET NULL,
    caller_agent_id   UUID REFERENCES agents(id) ON DELETE SET NULL,
    caller_user_id    UUID REFERENCES users(id) ON DELETE SET NULL,
    task_session_id   UUID REFERENCES task_sessions(id) ON DELETE SET NULL,
    skill_id          VARCHAR(128),
    state             VARCHAR(32) NOT NULL,
    status_message    JSONB,
    status_timestamp  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    protocol_version  VARCHAR(8) NOT NULL DEFAULT '1.0',
    binding           VARCHAR(16) NOT NULL,
    economics         JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key   VARCHAR(255) NOT NULL,
    federation_depth  SMALLINT NOT NULL DEFAULT 0,
    last_event_seq    INTEGER NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT a2a_tasks_state_valid CHECK (state IN (
        'TASK_STATE_SUBMITTED', 'TASK_STATE_WORKING', 'TASK_STATE_COMPLETED',
        'TASK_STATE_FAILED', 'TASK_STATE_CANCELED', 'TASK_STATE_REJECTED',
        'TASK_STATE_INPUT_REQUIRED', 'TASK_STATE_AUTH_REQUIRED')),
    CONSTRAINT a2a_tasks_binding_valid CHECK (binding IN ('JSONRPC', 'HTTP+JSON')),
    CONSTRAINT a2a_tasks_caller_present CHECK (caller_agent_id IS NOT NULL OR caller_user_id IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_a2a_tasks_idempotency ON a2a_tasks (idempotency_key);
CREATE UNIQUE INDEX IF NOT EXISTS uq_a2a_tasks_task_session ON a2a_tasks (task_session_id) WHERE task_session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_a2a_tasks_caller ON a2a_tasks (caller_agent_id, status_timestamp DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_a2a_tasks_caller_user ON a2a_tasks (caller_user_id, status_timestamp DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_a2a_tasks_tenant ON a2a_tasks (tenant_agent_id, status_timestamp DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_a2a_tasks_context ON a2a_tasks (context_id);
-- The reconciler's working set: open tasks backed by an economic session.
CREATE INDEX IF NOT EXISTS idx_a2a_tasks_open_linked ON a2a_tasks (updated_at)
    WHERE task_session_id IS NOT NULL AND state IN ('TASK_STATE_SUBMITTED', 'TASK_STATE_WORKING');

-- Client-facing protocol history ONLY (no prompts, reasoning, tool args, traces).
CREATE TABLE IF NOT EXISTS a2a_messages (
    id          BIGSERIAL PRIMARY KEY,
    task_id     UUID NOT NULL REFERENCES a2a_tasks(id) ON DELETE CASCADE,
    context_id  UUID NOT NULL,
    message_id  VARCHAR(128) NOT NULL,
    role        VARCHAR(16) NOT NULL,
    message     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT a2a_messages_role_valid CHECK (role IN ('ROLE_USER', 'ROLE_AGENT'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_a2a_messages_task_message ON a2a_messages (task_id, message_id);

CREATE TABLE IF NOT EXISTS a2a_artifacts (
    id           BIGSERIAL PRIMARY KEY,
    task_id      UUID NOT NULL REFERENCES a2a_tasks(id) ON DELETE CASCADE,
    artifact_id  VARCHAR(128) NOT NULL,
    artifact     JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_a2a_artifacts_task_artifact ON a2a_artifacts (task_id, artifact_id);

-- The durable event log every stream replays from. seq is per task, strictly
-- increasing, assigned under a row lock on a2a_tasks (last_event_seq).
CREATE TABLE IF NOT EXISTS a2a_task_events (
    task_id     UUID NOT NULL REFERENCES a2a_tasks(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    kind        VARCHAR(16) NOT NULL,
    payload     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (task_id, seq),
    CONSTRAINT a2a_task_events_kind_valid CHECK (kind IN ('status', 'artifact'))
);

-- Security/economic mutations. Append-only for EVERY caller (trigger below).
-- Ids carry no foreign keys: an audit row must outlive what it describes.
CREATE TABLE IF NOT EXISTS a2a_audit_log (
    id                BIGSERIAL PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    principal_class   VARCHAR(16) NOT NULL,
    principal_id      UUID,
    target_agent_id   UUID,
    operation         VARCHAR(48) NOT NULL,
    a2a_task_id       UUID,
    task_session_id   UUID,
    result            VARCHAR(48) NOT NULL,
    economics_action  VARCHAR(32),
    request_id        VARCHAR(64),
    detail            JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_a2a_audit_task ON a2a_audit_log (a2a_task_id);
CREATE INDEX IF NOT EXISTS idx_a2a_audit_created ON a2a_audit_log (created_at DESC);

CREATE OR REPLACE FUNCTION a2a_audit_log_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'a2a_audit_log is append-only (attempted %)', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_a2a_audit_log_append_only ON a2a_audit_log;
CREATE TRIGGER trg_a2a_audit_log_append_only
    BEFORE UPDATE OR DELETE ON a2a_audit_log
    FOR EACH ROW EXECUTE FUNCTION a2a_audit_log_append_only();

-- ── federation: AgentNet as an A2A client ─────────────────────────────────
-- A remote agent is catalog DATA. It never gets a wallet, reputation or
-- trusted status from its card; only an operator marks it 'verified'.
CREATE TABLE IF NOT EXISTS a2a_remote_agents (
    id                    UUID PRIMARY KEY,
    card_url              VARCHAR(2048) NOT NULL,
    host                  VARCHAR(255) NOT NULL,
    name                  VARCHAR(255),
    description           TEXT,
    provider_org          VARCHAR(255),
    provider_url          VARCHAR(2048),
    card_version          VARCHAR(64),
    card_hash             VARCHAR(64),
    etag                  VARCHAR(255),
    protocol_versions     JSONB NOT NULL DEFAULT '[]'::jsonb,
    bindings              JSONB NOT NULL DEFAULT '[]'::jsonb,
    skills                JSONB NOT NULL DEFAULT '[]'::jsonb,
    security              JSONB NOT NULL DEFAULT '{}'::jsonb,
    capabilities          JSONB NOT NULL DEFAULT '{}'::jsonb,
    state                 VARCHAR(16) NOT NULL DEFAULT 'discovered',
    state_reason          VARCHAR(255),
    last_fetch_at         TIMESTAMPTZ,
    last_validated_at     TIMESTAMPTZ,
    last_error_class      VARCHAR(64),
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    provenance            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT a2a_remote_agents_state_valid CHECK (state IN ('discovered', 'verified', 'degraded', 'quarantined', 'blocked'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_a2a_remote_agents_card_url ON a2a_remote_agents (card_url);
CREATE INDEX IF NOT EXISTS idx_a2a_remote_agents_state ON a2a_remote_agents (state);

CREATE TABLE IF NOT EXISTS a2a_remote_card_versions (
    id               BIGSERIAL PRIMARY KEY,
    remote_agent_id  UUID NOT NULL REFERENCES a2a_remote_agents(id) ON DELETE CASCADE,
    card_hash        VARCHAR(64) NOT NULL,
    card             JSONB NOT NULL,
    material_change  BOOLEAN NOT NULL DEFAULT FALSE,
    change_summary   JSONB NOT NULL DEFAULT '{}'::jsonb,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_a2a_remote_card_versions ON a2a_remote_card_versions (remote_agent_id, card_hash);

-- An operator-created way to call a remote agent. The credential is sealed
-- (Fernet, A2A_CREDENTIAL_KEY); cognition only ever sees the connection id.
CREATE TABLE IF NOT EXISTS a2a_connections (
    id                  UUID PRIMARY KEY,
    remote_agent_id     UUID NOT NULL REFERENCES a2a_remote_agents(id) ON DELETE CASCADE,
    label               VARCHAR(128) NOT NULL,
    auth_scheme         VARCHAR(16) NOT NULL DEFAULT 'none',
    sealed_credential   TEXT,
    daily_call_limit    INTEGER NOT NULL DEFAULT 20,
    created_by_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at          TIMESTAMPTZ,
    CONSTRAINT a2a_connections_scheme_valid CHECK (auth_scheme IN ('none', 'bearer'))
);
CREATE INDEX IF NOT EXISTS idx_a2a_connections_remote ON a2a_connections (remote_agent_id);

CREATE TABLE IF NOT EXISTS a2a_outbound_calls (
    id                 UUID PRIMARY KEY,
    connection_id      UUID REFERENCES a2a_connections(id) ON DELETE SET NULL,
    remote_agent_id    UUID REFERENCES a2a_remote_agents(id) ON DELETE SET NULL,
    initiator_class    VARCHAR(16) NOT NULL,
    initiator_id       UUID,
    correlation_id     UUID,
    causation_id       UUID,
    intent_id          UUID,
    depth              SMALLINT NOT NULL DEFAULT 1,
    operation          VARCHAR(32) NOT NULL,
    skill_id           VARCHAR(128),
    idempotency_key    VARCHAR(255) NOT NULL,
    remote_task_id     VARCHAR(255),
    remote_context_id  VARCHAR(255),
    remote_state       VARCHAR(32),
    status             VARCHAR(16) NOT NULL DEFAULT 'pending',
    error_class        VARCHAR(64),
    result_summary     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at        TIMESTAMPTZ,
    CONSTRAINT a2a_outbound_calls_status_valid CHECK (status IN ('pending', 'sent', 'succeeded', 'failed', 'timeout', 'refused'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_a2a_outbound_calls_idempotency ON a2a_outbound_calls (idempotency_key);
CREATE INDEX IF NOT EXISTS idx_a2a_outbound_calls_correlation ON a2a_outbound_calls (correlation_id);
CREATE INDEX IF NOT EXISTS idx_a2a_outbound_calls_remote_day ON a2a_outbound_calls (remote_agent_id, created_at DESC);
"""
