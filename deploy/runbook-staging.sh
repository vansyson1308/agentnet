#!/usr/bin/env bash
# AgentNet — staging deploy runbook
# Run this ON the server (root@139.180.143.222) after `git pull`.
# Idempotent: re-running upgrades safely; backs up the DB on every run.
#
# Usage:
#   cd /opt/agentnet
#   git fetch && git checkout claude/review-codebase-A4MMQ && git pull
#   bash deploy/runbook-staging.sh
#
# Requires .env at /opt/agentnet/.env with all production secrets set
# (see .env.production for the schema).

set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"
DATE_TAG="$(date +%F-%H%M)"

echo "=== AgentNet staging deploy @ $DATE_TAG ==="
echo "repo: $REPO"
echo "branch: $(git rev-parse --abbrev-ref HEAD)"
echo "commit: $(git rev-parse --short HEAD)"

# 1. Sanity: env file present and non-placeholder
if [[ ! -f .env ]]; then
    echo "FATAL: $REPO/.env missing. Copy .env.production and fill in real secrets."
    exit 1
fi
for var in POSTGRES_PASSWORD REDIS_PASSWORD JWT_SECRET_KEY FLASK_SECRET_KEY; do
    val=$(grep -E "^${var}=" .env | head -1 | cut -d= -f2-)
    if [[ -z "$val" || "$val" == *CHANGE_ME* || "$val" == "your_"* ]]; then
        echo "FATAL: .env '$var' is empty or a placeholder. Generate via: openssl rand -hex 32"
        exit 1
    fi
done
echo "✓ secrets look real"

# 2. Backup the SHARED postgres BEFORE staging touches the agentnet_staging DB.
mkdir -p /opt/agentnet/backups
BACKUP_FILE="/opt/agentnet/backups/pre-staging-deploy-${DATE_TAG}.sql.gz"
echo "→ dumping postgres to $BACKUP_FILE …"
docker exec agentnet-postgres pg_dumpall -U "${POSTGRES_USER:-agentnet}" \
    | gzip > "$BACKUP_FILE"
echo "✓ backup size: $(du -h "$BACKUP_FILE" | cut -f1)"

# 3. Tag the current code state as a rollback point.
git tag -f "rollback-staging-${DATE_TAG}"
echo "✓ git tag rollback-staging-${DATE_TAG}"

# 4. Ensure the staging DB exists. If not, create it; postgres-data
# bind mount means the DB persists across container restarts.
echo "→ ensuring agentnet_staging database exists"
docker exec agentnet-postgres psql -U "${POSTGRES_USER:-agentnet}" -tc \
    "SELECT 1 FROM pg_database WHERE datname='agentnet_staging'" \
    | grep -q 1 \
    || docker exec agentnet-postgres createdb -U "${POSTGRES_USER:-agentnet}" agentnet_staging

# 5. Bootstrap schema in staging DB if empty (init-db SQL files).
TABLE_COUNT=$(docker exec agentnet-postgres psql -U "${POSTGRES_USER:-agentnet}" -d agentnet_staging -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
if [[ "$TABLE_COUNT" -eq 0 ]]; then
    echo "→ staging DB is empty, applying init-db/*.sql"
    for f in services/registry/init-db/*.sql; do
        echo "  applying $(basename "$f")"
        docker exec -i agentnet-postgres psql -U "${POSTGRES_USER:-agentnet}" -d agentnet_staging -v ON_ERROR_STOP=1 < "$f"
    done
else
    echo "✓ staging DB already initialized ($TABLE_COUNT tables)"
fi

# 6. Build + up staging stack.
echo "→ docker compose -f docker-compose.yml -f docker-compose.staging.yml up -d --build"
docker compose -f docker-compose.yml -f docker-compose.staging.yml down
docker compose -f docker-compose.yml -f docker-compose.staging.yml up -d --build

# 7. Wait for staging healthchecks to settle.
echo "→ waiting for staging services to become healthy (max 180s)…"
deadline=$((SECONDS + 180))
while [[ $SECONDS -lt $deadline ]]; do
    bad=0
    for svc in agentnet-staging-registry agentnet-staging-payment agentnet-staging-dashboard agentnet-staging-society-worker; do
        status=$(docker inspect -f '{{.State.Health.Status}}' "$svc" 2>/dev/null || echo "missing")
        if [[ "$status" != "healthy" ]]; then
            bad=$((bad+1))
        fi
    done
    if [[ $bad -eq 0 ]]; then
        echo "✓ all staging services healthy"
        break
    fi
    sleep 5
done
docker compose -f docker-compose.yml -f docker-compose.staging.yml ps

# 8. Quick smoke from inside the box (Caddy may not be reloaded yet).
echo "→ smoke: registry on :8100"
curl -fsS --max-time 5 http://localhost:8100/healthz && echo
echo "→ smoke: payment on :8101"
curl -fsS --max-time 5 http://localhost:8101/healthz && echo
echo "→ smoke: dashboard on :8180"
curl -fsS --max-time 5 http://localhost:8180/healthz && echo

# 8b. Autonomous Society Runtime checks. The worker idles unless
# SOCIETY_RUNTIME_ENABLED=true is in .env; these checks hold either way.
# SKIP_SOCIETY_CHECKS=1 bypasses them (not recommended).
if [[ "${SKIP_SOCIETY_CHECKS:-0}" != "1" ]]; then
    echo "→ society: alembic head inside the staging registry"
    if docker exec agentnet-staging-registry alembic current 2>/dev/null | grep -q 0008_society_phase2; then
        echo "✓ alembic current = 0008_society_phase2"
    else
        echo "FATAL: staging registry is not at 0008_society_phase2 (entrypoint upgrade failed?)"
        exit 1
    fi
    echo "→ society: fresh + upgrade migration proof on scratch databases"
    bash deploy/society-migration-check.sh --mode docker --db-container agentnet-postgres --registry-container agentnet-staging-registry
    echo "→ society: worker health ($(docker inspect -f '{{.State.Health.Status}}' agentnet-staging-society-worker 2>/dev/null || echo missing))"
    echo "→ society: smoke on :8100 (operator checks + injection need SOCIETY_SMOKE_TOKEN in the environment)"
    SOCIETY_SMOKE_ARGS=(--api http://localhost:8100 --expect-runtime "${SOCIETY_RUNTIME_ENABLED:-false}" --metrics-probe 127.0.0.1:9101)
    if [[ -n "${SOCIETY_SMOKE_TOKEN:-}" ]]; then SOCIETY_SMOKE_ARGS+=(--inject); fi
    python3 deploy/society-staging-smoke.py "${SOCIETY_SMOKE_ARGS[@]}" --report "/opt/agentnet/backups/society-smoke-${DATE_TAG}.json"
fi

# 9. Reload Caddy so staging.agentnet.io.vn picks up the routing.
if systemctl is-active --quiet caddy; then
    cp deploy/Caddyfile /etc/caddy/Caddyfile
    systemctl reload caddy
    echo "✓ caddy reloaded"
fi

echo
echo "=== STAGING UP. Verify externally: ==="
echo "  curl -fsS https://staging.agentnet.io.vn/healthz"
echo "  curl -fsS https://staging.agentnet.io.vn/readyz"
echo "  curl -fsS https://staging.agentnet.io.vn/.well-known/agent-card.json | jq"
echo
echo "End-to-end money invariant smoke test (run from your laptop):"
echo "  REGISTRY_URL=https://staging.agentnet.io.vn \\"
echo "  PAYMENT_URL=https://staging.agentnet.io.vn \\"
echo "  python examples/orchestrator_agent/orchestrator.py"
echo
echo "Society runtime (see docs/SOCIETY_LIVE_MODEL_RUNBOOK.md):"
echo "  docker exec agentnet-staging-society-worker python -m app.society.canary preflight   # credential + provider probe"
echo "  SOCIETY_REDTEAM_TOKEN=... python3 deploy/society-staging-redteam.py --api http://localhost:8100"
echo "  SOCIETY_CANARY_TOKEN=...  docker exec -e SOCIETY_CANARY_TOKEN agentnet-staging-registry \\"
echo "      python -m app.society.canary observe --api http://localhost:8000 --scenario single"
echo
echo "Rollback if needed:"
echo "  git -C $REPO checkout rollback-staging-${DATE_TAG}"
echo "  docker compose -f docker-compose.yml -f docker-compose.staging.yml up -d --build"
