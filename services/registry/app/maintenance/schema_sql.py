"""Idempotent DDL for the Autonomous Maintenance OS (ADR-0010).

Single source of truth, consumed by:

* ``migrations/versions/0014_maintenance_os.py`` (+ ``0015_activity_turn_log.py``) -- existing databases, and
* ``init-db/19-maintenance-os.sql`` -- a fresh volume's bootstrap bundle
  (byte-identical; ``tests/maintenance/test_schema.py`` enforces it).

Additive only. Invariants the DATABASE enforces (not just the code):

* a non-terminal repair case always has ``next_action_at`` AND ``deadline_at``
  (``repair_cases_liveness``) -- the "nothing stranded" law is a constraint;
* at most one active repair case per incident (partial unique index; a
  resumable escalation still covers its incident);
* at most one open incident per fingerprint;
* at most one production maintenance release in flight, globally;
* plan revisions and artifacts are immutable, the transition audit log is
  append-only (triggers).
"""

MAINTENANCE_TERMINAL_STATES = (
    "AUTO_REPAIRED",
    "AUTO_ROLLED_BACK",
    "SAFELY_ESCALATED",
    "CANNOT_REPRODUCE",
    "DUPLICATE_RESOLVED",
    "POLICY_REFUSED",
)
MAINTENANCE_CASE_STATES = (
    "DETECTED",
    "CONFIRMED",
    "TRIAGED",
    "DIAGNOSING",
    "PLAN_READY",
    "BUILDING",
    "VERIFYING",
    "NEEDS_RESCOPE",
    "PROMOTING",
    "READY_FOR_RELEASE",
    "RELEASING",
    "POST_RELEASE_VERIFYING",
    "RECOVERY_PENDING",
) + MAINTENANCE_TERMINAL_STATES

MAINTENANCE_TABLES = (
    "maintenance_incidents",
    "maintenance_observations",
    "repair_cases",
    "repair_plan_revisions",
    "repair_attempts",
    "repair_activities",
    "repair_artifacts",
    "repair_evidence",
    "repair_transitions",
    "maintenance_releases",
    "maintenance_known_good",
    "maintenance_release_freezes",
    "maintenance_toil_events",
    "maintenance_heartbeats",
    "maintenance_knowledge",
)

_TERMINAL_SQL = ", ".join(f"'{s}'" for s in MAINTENANCE_TERMINAL_STATES)
_STATES_SQL = ", ".join(f"'{s}'" for s in MAINTENANCE_CASE_STATES)

MAINTENANCE_SQL = rf"""
-- ============================================================
-- Autonomous Maintenance OS (ADR-0010). Generated from
-- services/registry/app/maintenance/schema_sql.py -- edit THAT file.
-- ============================================================

-- An observed violation of a desired-state contract. Structural only.
CREATE TABLE IF NOT EXISTS maintenance_incidents (
    id                       UUID PRIMARY KEY,
    fingerprint              VARCHAR(64) NOT NULL,
    incident_class           VARCHAR(32) NOT NULL,
    priority                 VARCHAR(2) NOT NULL,
    severity                 VARCHAR(16) NOT NULL,
    source                   VARCHAR(64) NOT NULL,
    target                   VARCHAR(32) NOT NULL,
    desired_state_ref        VARCHAR(160) NOT NULL,
    affected_surfaces        JSONB NOT NULL DEFAULT '[]'::jsonb,
    status                   VARCHAR(16) NOT NULL DEFAULT 'open',
    first_observed_at        TIMESTAMPTZ NOT NULL,
    last_observed_at         TIMESTAMPTZ NOT NULL,
    observation_count        INTEGER NOT NULL DEFAULT 1,
    healthy_streak           INTEGER NOT NULL DEFAULT 0,
    current_evidence_digest  VARCHAR(64) NOT NULL,
    recurrence_of            UUID REFERENCES maintenance_incidents(id) ON DELETE SET NULL,
    recurrence_count         INTEGER NOT NULL DEFAULT 0,
    case_count               INTEGER NOT NULL DEFAULT 0,
    last_case_terminal_at    TIMESTAMPTZ,
    provenance               JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    opened_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at              TIMESTAMPTZ,
    closed_reason            VARCHAR(64),
    CONSTRAINT maintenance_incidents_status_valid CHECK (status IN ('open', 'recovered', 'closed')),
    CONSTRAINT maintenance_incidents_priority_valid CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
    CONSTRAINT maintenance_incidents_class_valid CHECK (incident_class IN (
        'AVAILABILITY', 'FUNCTIONAL_CONTRACT', 'AUTH', 'A2A', 'UI_NAVIGATION', 'UI_RENDERING',
        'ACCESSIBILITY', 'PERFORMANCE', 'SECURITY', 'DEPENDENCY', 'ECONOMIC_INVARIANT',
        'DATA_INVARIANT', 'RELEASE_REGRESSION', 'CONTROL_PLANE', 'EXTERNAL_DEPENDENCY'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_maintenance_incidents_open_fingerprint
    ON maintenance_incidents (fingerprint) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_maintenance_incidents_fingerprint ON maintenance_incidents (fingerprint, opened_at);

-- Trusted observations: evidence for incidents AND the SLI sample stream the
-- error budget is computed from. Never model text.
CREATE TABLE IF NOT EXISTS maintenance_observations (
    id                 BIGSERIAL PRIMARY KEY,
    incident_id        UUID REFERENCES maintenance_incidents(id) ON DELETE SET NULL,
    sli                VARCHAR(64) NOT NULL,
    target             VARCHAR(32) NOT NULL,
    source             VARCHAR(64) NOT NULL,
    collector_version  VARCHAR(32) NOT NULL,
    trust_class        VARCHAR(24) NOT NULL,
    ok                 BOOLEAN NOT NULL,
    fingerprint        VARCHAR(64),
    digest             VARCHAR(64) NOT NULL,
    payload            JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    observed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT maintenance_observations_trust_valid CHECK (trust_class IN (
        'trusted_probe', 'trusted_ci', 'trusted_provider', 'trusted_db', 'trusted_execution'))
);
CREATE INDEX IF NOT EXISTS idx_maintenance_observations_sli ON maintenance_observations (target, sli, observed_at);
CREATE INDEX IF NOT EXISTS idx_maintenance_observations_incident ON maintenance_observations (incident_id, observed_at);

-- One repair workflow for one incident. Only the reconciler writes state.
CREATE TABLE IF NOT EXISTS repair_cases (
    id                     UUID PRIMARY KEY,
    incident_id            UUID NOT NULL REFERENCES maintenance_incidents(id) ON DELETE CASCADE,
    state                  VARCHAR(32) NOT NULL,
    priority               VARCHAR(2) NOT NULL,
    risk_class             VARCHAR(24),
    repair_class           VARCHAR(32) NOT NULL,
    current_plan_revision  INTEGER NOT NULL DEFAULT 0,
    attempt_count          INTEGER NOT NULL DEFAULT 0,
    rescope_count          INTEGER NOT NULL DEFAULT 0,
    state_tries            INTEGER NOT NULL DEFAULT 0,
    model_cost_usd         NUMERIC(12, 6) NOT NULL DEFAULT 0,
    model_calls            INTEGER NOT NULL DEFAULT 0,
    started_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    state_entered_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    next_action_at         TIMESTAMPTZ,
    deadline_at            TIMESTAMPTZ,
    case_deadline_at       TIMESTAMPTZ NOT NULL,
    lease_owner            VARCHAR(128),
    lease_expires_at       TIMESTAMPTZ,
    terminal_reason        VARCHAR(64),
    terminal_at            TIMESTAMPTZ,
    resumable              BOOLEAN NOT NULL DEFAULT FALSE,
    escalation             JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    candidate_id           UUID REFERENCES code_candidates(id) ON DELETE SET NULL,
    promotion_id           UUID REFERENCES code_promotions(id) ON DELETE SET NULL,
    release_id             UUID,
    base_sha               VARCHAR(64),
    head_sha               VARCHAR(64),
    merged_sha             VARCHAR(64),
    facts                  JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    version                INTEGER NOT NULL DEFAULT 0,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT repair_cases_state_valid CHECK (state IN ({_STATES_SQL})),
    CONSTRAINT repair_cases_priority_valid CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
    CONSTRAINT repair_cases_liveness CHECK (state IN ({_TERMINAL_SQL}) OR (next_action_at IS NOT NULL AND deadline_at IS NOT NULL)),
    CONSTRAINT repair_cases_terminal_stamped CHECK (state NOT IN ({_TERMINAL_SQL}) OR (terminal_at IS NOT NULL AND terminal_reason IS NOT NULL)),
    CONSTRAINT repair_cases_budgets_nonnegative CHECK (attempt_count >= 0 AND rescope_count >= 0 AND model_cost_usd >= 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_repair_cases_one_active_per_incident
    ON repair_cases (incident_id)
    WHERE state NOT IN ({_TERMINAL_SQL}) OR (state = 'SAFELY_ESCALATED' AND resumable);
CREATE INDEX IF NOT EXISTS idx_repair_cases_due ON repair_cases (next_action_at) WHERE state NOT IN ({_TERMINAL_SQL});
CREATE INDEX IF NOT EXISTS idx_repair_cases_state ON repair_cases (state, priority);

-- Immutable plan revisions. A re-scope creates revision N+1; N never widens.
CREATE TABLE IF NOT EXISTS repair_plan_revisions (
    id                  UUID PRIMARY KEY,
    case_id             UUID NOT NULL REFERENCES repair_cases(id) ON DELETE CASCADE,
    revision            INTEGER NOT NULL,
    parent_revision     INTEGER,
    files_allowed       JSONB NOT NULL DEFAULT '[]'::jsonb,
    acceptance_tests    JSONB NOT NULL DEFAULT '[]'::jsonb,
    contract_refs       JSONB NOT NULL DEFAULT '[]'::jsonb,
    root_cause          TEXT NOT NULL DEFAULT '',
    approach            TEXT NOT NULL DEFAULT '',
    risk_class          VARCHAR(24) NOT NULL,
    risk_reasons        JSONB NOT NULL DEFAULT '[]'::jsonb,
    rescope_reason      VARCHAR(64),
    author_activity_id  UUID,
    digest              VARCHAR(64) NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT repair_plan_revisions_positive CHECK (revision >= 1)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_repair_plan_revisions_case_revision ON repair_plan_revisions (case_id, revision);

-- One bounded Builder attempt against one plan revision.
CREATE TABLE IF NOT EXISTS repair_attempts (
    id              UUID PRIMARY KEY,
    case_id         UUID NOT NULL REFERENCES repair_cases(id) ON DELETE CASCADE,
    attempt         INTEGER NOT NULL,
    plan_revision   INTEGER NOT NULL,
    base_sha        VARCHAR(64),
    head_sha        VARCHAR(64),
    patch_digest    VARCHAR(64),
    turns           INTEGER NOT NULL DEFAULT 0,
    test_runs       INTEGER NOT NULL DEFAULT 0,
    outcome         VARCHAR(24) NOT NULL DEFAULT 'running',
    feedback        JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    CONSTRAINT repair_attempts_outcome_valid CHECK (outcome IN (
        'running', 'patch_ready', 'needs_rescope', 'failed', 'verified', 'rejected'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_repair_attempts_case_attempt ON repair_attempts (case_id, attempt);

-- One try of one cognitive activity (a model call loop). Idempotent per key.
CREATE TABLE IF NOT EXISTS repair_activities (
    id                UUID PRIMARY KEY,
    case_id           UUID NOT NULL REFERENCES repair_cases(id) ON DELETE CASCADE,
    kind              VARCHAR(32) NOT NULL,
    role              VARCHAR(32) NOT NULL,
    plan_revision     INTEGER NOT NULL DEFAULT 0,
    attempt           INTEGER NOT NULL DEFAULT 0,
    try_number        INTEGER NOT NULL DEFAULT 1,
    idempotency_key   VARCHAR(200) NOT NULL,
    status            VARCHAR(16) NOT NULL DEFAULT 'running',
    input_digest      VARCHAR(64) NOT NULL,
    output_digest     VARCHAR(64),
    artifact_id       UUID,
    error_class       VARCHAR(48),
    error             VARCHAR(500),
    model_provider    VARCHAR(32),
    model_name        VARCHAR(64),
    tokens_in         INTEGER NOT NULL DEFAULT 0,
    tokens_out        INTEGER NOT NULL DEFAULT 0,
    cost_usd          NUMERIC(12, 6) NOT NULL DEFAULT 0,
    turns             INTEGER NOT NULL DEFAULT 0,
    lease_owner       VARCHAR(128),
    lease_expires_at  TIMESTAMPTZ,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at       TIMESTAMPTZ,
    CONSTRAINT repair_activities_status_valid CHECK (status IN ('running', 'succeeded', 'failed', 'abandoned')),
    CONSTRAINT repair_activities_kind_valid CHECK (kind IN (
        'DiagnoseIncident', 'DesignRepair', 'AuthorPatch', 'ReviewPatch', 'SecurityReview',
        'ExplainEscalation', 'DraftPostmortem'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_repair_activities_idempotency ON repair_activities (idempotency_key);
CREATE INDEX IF NOT EXISTS idx_repair_activities_case ON repair_activities (case_id, kind, started_at);
CREATE INDEX IF NOT EXISTS idx_repair_activities_day ON repair_activities (started_at);

-- Immutable artifacts: diagnoses, patch sets, reviews, escalation packages,
-- postmortems. trust_class says whether it is fact or model hypothesis.
CREATE TABLE IF NOT EXISTS repair_artifacts (
    id             UUID PRIMARY KEY,
    case_id        UUID NOT NULL REFERENCES repair_cases(id) ON DELETE CASCADE,
    kind           VARCHAR(32) NOT NULL,
    digest         VARCHAR(64) NOT NULL,
    trust_class    VARCHAR(24) NOT NULL,
    plan_revision  INTEGER NOT NULL DEFAULT 0,
    attempt        INTEGER NOT NULL DEFAULT 0,
    produced_by    VARCHAR(128) NOT NULL,
    content        JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_repair_artifacts_case_kind_digest ON repair_artifacts (case_id, kind, digest);

-- Evidence linked to a case, with provenance (source, collector, digest, trust).
CREATE TABLE IF NOT EXISTS repair_evidence (
    id                 UUID PRIMARY KEY,
    case_id            UUID NOT NULL REFERENCES repair_cases(id) ON DELETE CASCADE,
    kind               VARCHAR(48) NOT NULL,
    source             VARCHAR(64) NOT NULL,
    collector_version  VARCHAR(32) NOT NULL,
    trust_class        VARCHAR(24) NOT NULL,
    digest             VARCHAR(64) NOT NULL,
    payload            JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    collected_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_repair_evidence_case_digest ON repair_evidence (case_id, kind, digest);

-- Append-only audit of every state transition.
CREATE TABLE IF NOT EXISTS repair_transitions (
    id               BIGSERIAL PRIMARY KEY,
    case_id          UUID NOT NULL REFERENCES repair_cases(id) ON DELETE CASCADE,
    from_state       VARCHAR(32),
    to_state         VARCHAR(32) NOT NULL,
    actor_type       VARCHAR(16) NOT NULL,
    actor_id         VARCHAR(128) NOT NULL,
    reason_code      VARCHAR(64) NOT NULL,
    evidence_digest  VARCHAR(64),
    detail           JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_repair_transitions_case ON repair_transitions (case_id, id);

-- A production maintenance release. Only the Release Controller writes it.
CREATE TABLE IF NOT EXISTS maintenance_releases (
    id                     UUID PRIMARY KEY,
    case_id                UUID NOT NULL REFERENCES repair_cases(id) ON DELETE CASCADE,
    incident_id            UUID NOT NULL REFERENCES maintenance_incidents(id) ON DELETE CASCADE,
    status                 VARCHAR(24) NOT NULL DEFAULT 'pending',
    risk_class             VARCHAR(24) NOT NULL,
    head_sha               VARCHAR(64) NOT NULL,
    tree_sha               VARCHAR(64),
    attestation            JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    attestation_digest     VARCHAR(64) NOT NULL,
    attestation_signature  VARCHAR(128) NOT NULL,
    verification           JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    services               JSONB NOT NULL DEFAULT '[]'::jsonb,
    release_branch         VARCHAR(255),
    pr_number              INTEGER,
    pr_url                 VARCHAR(512),
    production_merge_sha   VARCHAR(64),
    deployments            JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    known_good_id          UUID,
    rollback               JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    healthy_streak         INTEGER NOT NULL DEFAULT 0,
    failure_reason         VARCHAR(500),
    attempt                INTEGER NOT NULL DEFAULT 0,
    lease_owner            VARCHAR(128),
    lease_expires_at       TIMESTAMPTZ,
    next_action_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deadline_at            TIMESTAMPTZ NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at           TIMESTAMPTZ,
    CONSTRAINT maintenance_releases_status_valid CHECK (status IN (
        'pending', 'verifying', 'preview_validating', 'pr_open', 'merged', 'deploying',
        'post_deploy_verifying', 'succeeded', 'rolling_back', 'rolled_back', 'rollback_failed', 'refused'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_maintenance_releases_case_sha ON maintenance_releases (case_id, head_sha);
CREATE UNIQUE INDEX IF NOT EXISTS uq_maintenance_releases_one_in_flight
    ON maintenance_releases ((TRUE))
    WHERE status NOT IN ('succeeded', 'rolled_back', 'rollback_failed', 'refused');

-- Known-good production releases: the rollback source of truth.
CREATE TABLE IF NOT EXISTS maintenance_known_good (
    id              UUID PRIMARY KEY,
    environment     VARCHAR(32) NOT NULL,
    production_sha  VARCHAR(64) NOT NULL,
    tree_sha        VARCHAR(64),
    deployments     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    config_digest   VARCHAR(64),
    evidence        JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    source          VARCHAR(64) NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retired_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_maintenance_known_good_env ON maintenance_known_good (environment, recorded_at);

-- Production release freezes (rollback parity, P0, rollback failure, owner).
CREATE TABLE IF NOT EXISTS maintenance_release_freezes (
    id           UUID PRIMARY KEY,
    reason_code  VARCHAR(64) NOT NULL,
    detail       JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    owner_only   BOOLEAN NOT NULL DEFAULT TRUE,
    opened_by    VARCHAR(128) NOT NULL,
    opened_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lifted_at    TIMESTAMPTZ,
    lifted_by    VARCHAR(128),
    lift_reason  VARCHAR(255)
);
CREATE INDEX IF NOT EXISTS idx_maintenance_release_freezes_open ON maintenance_release_freezes (lifted_at);

-- Human/operator interventions (toil). The OS drives GREEN toil to zero.
CREATE TABLE IF NOT EXISTS maintenance_toil_events (
    id          UUID PRIMARY KEY,
    kind        VARCHAR(40) NOT NULL,
    case_id     UUID REFERENCES repair_cases(id) ON DELETE SET NULL,
    actor       VARCHAR(128) NOT NULL,
    detail      JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT maintenance_toil_kind_valid CHECK (kind IN (
        'manual_merge', 'manual_release', 'manual_candidate_abandon', 'manual_memory_correction',
        'manual_repair', 'owner_approval', 'owner_refusal', 'freeze_lift'))
);

-- Controller liveness for the kernel watchdog.
CREATE TABLE IF NOT EXISTS maintenance_heartbeats (
    component        VARCHAR(48) PRIMARY KEY,
    worker_id        VARCHAR(128) NOT NULL,
    beat_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cycles           BIGINT NOT NULL DEFAULT 0,
    errors           BIGINT NOT NULL DEFAULT 0,
    last_error_class VARCHAR(64),
    last_error_at    TIMESTAMPTZ,
    details          JSONB NOT NULL DEFAULT '{{}}'::jsonb
);

-- Trusted maintenance knowledge derived from terminal cases (never model
-- chain-of-thought; root_cause is labelled as the accepted plan hypothesis).
CREATE TABLE IF NOT EXISTS maintenance_knowledge (
    id                    UUID PRIMARY KEY,
    case_id               UUID NOT NULL REFERENCES repair_cases(id) ON DELETE CASCADE,
    incident_fingerprint  VARCHAR(64) NOT NULL,
    incident_class        VARCHAR(32) NOT NULL,
    outcome               VARCHAR(32) NOT NULL,
    root_cause            TEXT NOT NULL DEFAULT '',
    repair_digest         VARCHAR(64),
    tests_added           JSONB NOT NULL DEFAULT '[]'::jsonb,
    monitors              JSONB NOT NULL DEFAULT '[]'::jsonb,
    release_result        VARCHAR(32),
    rollback_result       VARCHAR(32),
    detector_gap          JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_maintenance_knowledge_case ON maintenance_knowledge (case_id);
CREATE INDEX IF NOT EXISTS idx_maintenance_knowledge_fingerprint ON maintenance_knowledge (incident_fingerprint);

-- Immutability: plan revisions and artifacts never change; the transition
-- log only grows.
CREATE OR REPLACE FUNCTION maintenance_immutable_row() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is immutable/append-only (attempted %)', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_repair_plan_revisions_immutable ON repair_plan_revisions;
CREATE TRIGGER trg_repair_plan_revisions_immutable
    BEFORE UPDATE ON repair_plan_revisions
    FOR EACH ROW EXECUTE FUNCTION maintenance_immutable_row();

DROP TRIGGER IF EXISTS trg_repair_artifacts_immutable ON repair_artifacts;
CREATE TRIGGER trg_repair_artifacts_immutable
    BEFORE UPDATE ON repair_artifacts
    FOR EACH ROW EXECUTE FUNCTION maintenance_immutable_row();

DROP TRIGGER IF EXISTS trg_repair_transitions_append_only ON repair_transitions;
CREATE TRIGGER trg_repair_transitions_append_only
    BEFORE UPDATE ON repair_transitions
    FOR EACH ROW EXECUTE FUNCTION maintenance_immutable_row();
"""

#: Migration 0015: structural per-turn activity telemetry (tool, bytes, tokens; never text).
MAINTENANCE_TURN_LOG_SQL = "ALTER TABLE repair_activities ADD COLUMN IF NOT EXISTS turn_log JSONB NOT NULL DEFAULT '[]'::jsonb;\n"
MAINTENANCE_SQL = MAINTENANCE_SQL + "\n-- 0015_activity_turn_log\n" + MAINTENANCE_TURN_LOG_SQL

__all__ = ["MAINTENANCE_SQL", "MAINTENANCE_TURN_LOG_SQL", "MAINTENANCE_TABLES", "MAINTENANCE_CASE_STATES", "MAINTENANCE_TERMINAL_STATES"]
