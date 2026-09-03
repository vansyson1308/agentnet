#!/usr/bin/env bash
# deploy/society-migration-check.sh — prove the society migrations on SCRATCH databases.
#
#   FRESH   : full init-db bundle (what a new Postgres volume gets) → stamp 0003 →
#             `alembic upgrade head` must be idempotent over the already-present
#             society DDL and land on 0008_society_phase2.
#   UPGRADE : pre-society bundle (init-db minus 16-society-runtime.sql) → stamp 0003 →
#             `alembic upgrade head` must run 0007 and 0008; a second `upgrade head`
#             must be a no-op; `downgrade 0007_society_runtime` → `upgrade head` must
#             round-trip (skip with --no-downgrade).
#   Both    : table/column/index checks for intent_approvals, users.society_role,
#             agent_runs.model_*, agent_intents.resume_*.
#
# Usage:
#   bash deploy/society-migration-check.sh --mode local    # psql + alembic on this host (dev/CI)
#   bash deploy/society-migration-check.sh --mode docker   # on the staging host (docker exec)
# Options:
#   --db-container NAME        (docker) postgres container, default agentnet-postgres
#   --registry-container NAME  (docker) registry container with alembic, default agentnet-staging-registry
#   --keep                     leave the scratch databases behind
#   --no-downgrade             skip the downgrade/upgrade round-trip
# Scratch DBs are named agentnet_migcheck_{fresh,upgrade}_<epoch>. Nothing else is touched.
# Env (local mode): POSTGRES_HOST/PORT/USER/PASSWORD as for the services.

set -euo pipefail

MODE="docker"
DB_CONTAINER="agentnet-postgres"
REG_CONTAINER="agentnet-staging-registry"
KEEP=0
WITH_DOWNGRADE=1
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode) MODE="$2"; shift 2 ;;
        --db-container) DB_CONTAINER="$2"; shift 2 ;;
        --registry-container) REG_CONTAINER="$2"; shift 2 ;;
        --keep) KEEP=1; shift ;;
        --no-downgrade) WITH_DOWNGRADE=0; shift ;;
        -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[[ "$MODE" == "docker" || "$MODE" == "local" ]] || { echo "--mode must be docker|local" >&2; exit 2; }

cd "$(dirname "$0")/.."
INIT_DIR="services/registry/init-db"
EXPECTED_HEAD="0008_society_phase2"
PG_USER="${POSTGRES_USER:-agentnet}"
PG_HOST="${POSTGRES_HOST:-127.0.0.1}"
PG_PORT="${POSTGRES_PORT:-5432}"
PG_PASS="${POSTGRES_PASSWORD:-}"
TAG="$(date +%s)"
FRESH_DB="agentnet_migcheck_fresh_${TAG}"
UPGRADE_DB="agentnet_migcheck_upgrade_${TAG}"
FAILURES=0

ok()   { echo "  ✓ $*"; }
bad()  { echo "  ✗ $*" >&2; FAILURES=$((FAILURES + 1)); }
die()  { echo "FATAL: $*" >&2; exit 1; }

run_psql() {  # run_psql DB [psql args...]  (stdin passes through)
    local db="$1"; shift
    if [[ "$MODE" == "docker" ]]; then
        docker exec -i "$DB_CONTAINER" psql -U "$PG_USER" -d "$db" -v ON_ERROR_STOP=1 -q "$@"
    else
        PGPASSWORD="$PG_PASS" psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db" -v ON_ERROR_STOP=1 -q "$@"
    fi
}
scalar() { run_psql "$1" -tA -c "$2"; }
run_alembic() {  # run_alembic DB args...  (stdout+stderr merged; alembic logs to stderr)
    local db="$1"; shift
    if [[ "$MODE" == "docker" ]]; then
        docker exec -e POSTGRES_DB="$db" "$REG_CONTAINER" alembic "$@" 2>&1
    else
        (cd services/registry && env POSTGRES_DB="$db" POSTGRES_HOST="$PG_HOST" POSTGRES_PORT="$PG_PORT" \
            POSTGRES_USER="$PG_USER" POSTGRES_PASSWORD="$PG_PASS" JAEGER_ENABLED=false \
            ENVIRONMENT="${ENVIRONMENT:-development}" alembic "$@" 2>&1)
    fi
}
create_db() { run_psql postgres -c "CREATE DATABASE \"$1\"" >/dev/null; }
drop_db()   { run_psql postgres -c "DROP DATABASE IF EXISTS \"$1\"" >/dev/null 2>&1 || true; }
cleanup() {
    if [[ "$KEEP" == "1" ]]; then
        echo "keeping scratch databases: $FRESH_DB $UPGRADE_DB"
    else
        drop_db "$FRESH_DB"; drop_db "$UPGRADE_DB"
    fi
}
trap cleanup EXIT

apply_bundle() {  # apply_bundle DB skip_society(0|1)
    local db="$1" skip="$2" f
    for f in "$INIT_DIR"/*.sql; do
        if [[ "$skip" == "1" && "$(basename "$f")" == "16-society-runtime.sql" ]]; then continue; fi
        run_psql "$db" < "$f" >/dev/null
    done
}

check_schema() {  # check_schema DB
    local db="$1" spec t c n
    for spec in intent_approvals:id intent_approvals:intent_id intent_approvals:decision intent_approvals:final_state \
                users:society_role agent_runs:model_requests agent_runs:model_retries agent_runs:model_timeouts \
                agent_intents:resume_worker_id agent_intents:resume_lease_expires_at agent_intents:resume_attempt \
                society_events:idempotency_key agent_capability_grants:approval_required_intents code_candidates:status; do
        t="${spec%%:*}"; c="${spec##*:}"
        n="$(scalar "$db" "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='$t' AND column_name='$c'")"
        if [[ "$n" == "1" ]]; then ok "$db: $t.$c"; else bad "$db: missing column $t.$c"; fi
    done
    for spec in idx_agent_intents_resumable idx_intent_approvals_decision idx_society_events_actor_created; do
        n="$(scalar "$db" "SELECT count(*) FROM pg_indexes WHERE schemaname='public' AND indexname='$spec'")"
        if [[ "$n" == "1" ]]; then ok "$db: index $spec"; else bad "$db: missing index $spec"; fi
    done
    n="$(scalar "$db" "SELECT count(*) FROM pg_constraint WHERE conname LIKE 'intent_approvals_intent_id%' AND contype IN ('u','p')")"
    if [[ "$n" -ge 1 ]]; then ok "$db: intent_approvals.intent_id is unique"; else bad "$db: intent_approvals.intent_id uniqueness missing"; fi
}
expect_head() {
    local db="$1" cur
    cur="$(run_alembic "$db" current || true)"
    if [[ "$cur" == *"$EXPECTED_HEAD"* ]]; then ok "$db: alembic current = $EXPECTED_HEAD"; else bad "$db: alembic current is not $EXPECTED_HEAD: $cur"; fi
}
expect_noop_upgrade() {
    local db="$1" out
    out="$(run_alembic "$db" upgrade head)"
    if [[ "$out" == *"Running upgrade"* ]]; then bad "$db: second upgrade re-ran migrations (version stamp not persisted)"; else ok "$db: second upgrade head is a no-op"; fi
}

echo "=== society migration check (mode=$MODE, expected head=$EXPECTED_HEAD) ==="

echo "→ FRESH path: full init-db bundle + alembic"
create_db "$FRESH_DB"
apply_bundle "$FRESH_DB" 0
n="$(scalar "$FRESH_DB" "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='intent_approvals'")"
[[ "$n" == "1" ]] && ok "$FRESH_DB: bundle created intent_approvals" || bad "$FRESH_DB: bundle did not create intent_approvals"
run_alembic "$FRESH_DB" stamp 0003_spending_cap_fix >/dev/null
out="$(run_alembic "$FRESH_DB" upgrade head)" || { echo "$out"; die "$FRESH_DB: alembic upgrade head failed"; }
[[ "$out" == *"-> $EXPECTED_HEAD"* ]] && ok "$FRESH_DB: upgrade reached $EXPECTED_HEAD over existing DDL (idempotent)" || bad "$FRESH_DB: upgrade output lacks $EXPECTED_HEAD: $out"
expect_head "$FRESH_DB"
check_schema "$FRESH_DB"
expect_noop_upgrade "$FRESH_DB"

echo "→ UPGRADE path: pre-society bundle + alembic 0004..0008"
create_db "$UPGRADE_DB"
apply_bundle "$UPGRADE_DB" 1
n="$(scalar "$UPGRADE_DB" "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='society_events'")"
[[ "$n" == "0" ]] && ok "$UPGRADE_DB: starts without society tables" || bad "$UPGRADE_DB: society tables present before migration"
run_alembic "$UPGRADE_DB" stamp 0003_spending_cap_fix >/dev/null
out="$(run_alembic "$UPGRADE_DB" upgrade head)" || { echo "$out"; die "$UPGRADE_DB: alembic upgrade head failed"; }
[[ "$out" == *"0006_email_verified -> 0007_society_runtime"* ]] && ok "$UPGRADE_DB: ran 0007_society_runtime" || bad "$UPGRADE_DB: 0007 did not run: $out"
[[ "$out" == *"0007_society_runtime -> $EXPECTED_HEAD"* ]] && ok "$UPGRADE_DB: ran $EXPECTED_HEAD" || bad "$UPGRADE_DB: 0008 did not run: $out"
expect_head "$UPGRADE_DB"
check_schema "$UPGRADE_DB"
expect_noop_upgrade "$UPGRADE_DB"
if [[ "$WITH_DOWNGRADE" == "1" ]]; then
    echo "→ UPGRADE path: downgrade 0007 → upgrade head round-trip"
    out="$(run_alembic "$UPGRADE_DB" downgrade 0007_society_runtime)" || { echo "$out"; die "$UPGRADE_DB: downgrade failed"; }
    n="$(scalar "$UPGRADE_DB" "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='intent_approvals'")"
    [[ "$n" == "0" ]] && ok "$UPGRADE_DB: downgrade removed intent_approvals" || bad "$UPGRADE_DB: intent_approvals survived downgrade"
    n="$(scalar "$UPGRADE_DB" "SELECT count(*) FROM information_schema.columns WHERE table_name='users' AND column_name='society_role'")"
    [[ "$n" == "0" ]] && ok "$UPGRADE_DB: downgrade removed users.society_role" || bad "$UPGRADE_DB: users.society_role survived downgrade"
    out="$(run_alembic "$UPGRADE_DB" upgrade head)" || { echo "$out"; die "$UPGRADE_DB: re-upgrade failed"; }
    expect_head "$UPGRADE_DB"
    check_schema "$UPGRADE_DB"
fi

echo
if [[ "$FAILURES" -eq 0 ]]; then
    echo "=== SOCIETY MIGRATION CHECK: PASS (fresh + upgrade paths at $EXPECTED_HEAD) ==="
else
    echo "=== SOCIETY MIGRATION CHECK: FAIL ($FAILURES problem(s)) ===" >&2
    exit 1
fi
