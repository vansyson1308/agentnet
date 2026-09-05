"""Idempotent DDL for the tables that existed only as ORM models.

Single source of truth, consumed by BOTH:

* ``services/registry/migrations/versions/0009_app_tables.py`` — the alembic
  migration existing databases apply on container start, and
* ``services/registry/init-db/17-app-tables.sql`` — the bootstrap bundle
  Postgres runs on a *fresh* volume (``tests/test_db_parity.py`` asserts the
  file is byte-identical to ``APP_TABLES_SQL``).

Until this module existed these eight tables had NO DDL anywhere: they were
only created if something called ``Base.metadata.create_all`` (nothing in
the services does), so a fresh deployment had ORM models pointing at tables
that did not exist. Column names / types / nullability / defaults / foreign
keys mirror the ORM definitions exactly:

* registry ``app/models.py``: ``audit_log``, ``provisioning_providers``,
  ``provisioning_services``, ``projects``, ``scoped_tokens``,
  ``project_resources``, ``orchestrator_partners``;
* payment ``app/models.py``: ``approval_requests`` (``amount`` is BIGINT like
  every other money column; ``currency`` uses the shared ``currency_type``
  enum type from init-db/01-init.sql).

Timestamps are TIMESTAMPTZ (these tables were never part of the naive
TIMESTAMP core created by 01-init.sql). Every statement is ``IF NOT
EXISTS`` so running it twice — fresh DB via init-db, then ``alembic upgrade
head`` on top — is a no-op the second time. Indexes beyond the ORM's
``index=True`` / ``unique=True`` are operational extras (they do not affect
ORM parity). See docs/DATABASE_SCHEMA_CONTRACT.md.
"""

APP_TABLES_SQL = r"""
-- ============================================================
-- Application tables that previously existed only as ORM models
-- (registry: audit_log, provisioning catalog, projects, scoped tokens,
--  orchestrator partners; payment: approval_requests).
-- Generated from services/registry/app/schema_app_sql.py — edit THAT file.
-- ============================================================

-- Security event log (Phase S1). Written by auth / admin routes.
CREATE TABLE IF NOT EXISTS audit_log (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id    UUID,
    actor_ip         VARCHAR,
    action           VARCHAR NOT NULL,
    target_id        VARCHAR,
    payload_summary  TEXT,
    success          BOOLEAN DEFAULT TRUE,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log (actor_user_id);

-- AgentNet Provisioning Protocol (APP): provider catalog.
CREATE TABLE IF NOT EXISTS provisioning_providers (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug         VARCHAR NOT NULL UNIQUE,
    name         VARCHAR NOT NULL,
    description  TEXT,
    website      VARCHAR,
    logo_url     VARCHAR,
    is_active    BOOLEAN DEFAULT TRUE,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS provisioning_services (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id      UUID NOT NULL REFERENCES provisioning_providers(id) ON DELETE CASCADE,
    service_name     VARCHAR NOT NULL,
    description      TEXT,
    category         VARCHAR NOT NULL,
    tier             VARCHAR NOT NULL DEFAULT 'free',
    pricing_credits  INTEGER DEFAULT 0,
    pricing_usdc     NUMERIC(12, 6) DEFAULT 0,
    regions          JSONB DEFAULT '[]'::jsonb,
    required_params  JSONB DEFAULT '[]'::jsonb,
    output_params    JSONB DEFAULT '{}'::jsonb,
    is_active        BOOLEAN DEFAULT TRUE,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_provisioning_services_category ON provisioning_services (category);
CREATE INDEX IF NOT EXISTS idx_provisioning_services_provider ON provisioning_services (provider_id);

-- Persistent resource grouping (mirrors Stripe Projects' state.json).
CREATE TABLE IF NOT EXISTS projects (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         VARCHAR NOT NULL,
    agent_id     UUID REFERENCES agents(id) ON DELETE CASCADE,
    description  TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_projects_agent ON projects (agent_id);

-- Scoped API tokens — per-resource, per-agent credentials with limits.
CREATE TABLE IF NOT EXISTS scoped_tokens (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash       VARCHAR NOT NULL,
    agent_id         UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    resource_type    VARCHAR NOT NULL,
    resource_id      VARCHAR,
    project_id       UUID REFERENCES projects(id) ON DELETE SET NULL,
    spending_cap     INTEGER NOT NULL DEFAULT 100,
    total_spent      INTEGER NOT NULL DEFAULT 0,
    allowed_actions  JSONB DEFAULT '[]'::jsonb,
    expires_at       TIMESTAMPTZ,
    is_revoked       BOOLEAN DEFAULT FALSE,
    revoked_at       TIMESTAMPTZ,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_scoped_tokens_token_hash ON scoped_tokens (token_hash);
CREATE INDEX IF NOT EXISTS idx_scoped_tokens_agent ON scoped_tokens (agent_id);
CREATE INDEX IF NOT EXISTS idx_scoped_tokens_project ON scoped_tokens (project_id);

CREATE TABLE IF NOT EXISTS project_resources (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id       UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    resource_type    VARCHAR NOT NULL,
    resource_ref     VARCHAR,
    provider         VARCHAR,
    status           VARCHAR DEFAULT 'provisioned',
    scoped_token_id  UUID REFERENCES scoped_tokens(id) ON DELETE SET NULL,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_project_resources_project ON project_resources (project_id);

-- Third-party platforms that provision AgentNet accounts for their users.
CREATE TABLE IF NOT EXISTS orchestrator_partners (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                VARCHAR NOT NULL,
    platform_url        VARCHAR,
    webhook_url         VARCHAR,
    client_id           VARCHAR NOT NULL UNIQUE,
    client_secret_hash  VARCHAR NOT NULL,
    is_active           BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Payment service: human approval requests for agent spending / escrow.
-- Money invariant: this table never moves funds; approving a request only
-- unblocks the normal escrow path (wallet balances change via triggers).
CREATE TABLE IF NOT EXISTS approval_requests (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id         UUID NOT NULL REFERENCES agents(id),
    user_id          UUID NOT NULL REFERENCES users(id),
    amount           BIGINT NOT NULL,
    currency         currency_type NOT NULL DEFAULT 'credits',
    description      TEXT NOT NULL,
    callback_url     VARCHAR,
    status           VARCHAR(16) DEFAULT 'pending',
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    responded_at     TIMESTAMPTZ,
    task_session_id  UUID REFERENCES task_sessions(id),
    expires_at       TIMESTAMPTZ,
    approved_at      TIMESTAMPTZ,
    denied_at        TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_approval_requests_user_status ON approval_requests (user_id, status);
CREATE INDEX IF NOT EXISTS idx_approval_requests_agent ON approval_requests (agent_id);
CREATE INDEX IF NOT EXISTS idx_approval_requests_task ON approval_requests (task_session_id);
"""

# Drop order for the migration's downgrade (children before parents).
APP_TABLES = (
    "approval_requests",
    "orchestrator_partners",
    "project_resources",
    "scoped_tokens",
    "projects",
    "provisioning_services",
    "provisioning_providers",
    "audit_log",
)
