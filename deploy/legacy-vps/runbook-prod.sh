#!/usr/bin/env bash
# =============================================================================
# LEGACY — DO NOT USE FOR CURRENT DEPLOYMENT
#
# This script belongs to the retired single-VPS / SSH deployment model
# (fixed host, root shell, /opt/agentnet checkout, Caddy in front, combined
# `docker compose -f docker-compose.yml -f docker-compose.prod.yml` project).
# It is kept as operational history only. The current, hosting-neutral
# procedure is docs/DEPLOYMENT_ARCHITECTURE.md; staging is the standalone
# Compose project in docker-compose.staging.yml.
#
# The guard below makes accidental execution impossible.
# =============================================================================
if [[ "${AGENTNET_ALLOW_LEGACY_VPS:-}" != "I_UNDERSTAND_THIS_IS_RETIRED" ]]; then
    echo "REFUSING: $(basename "$0") is a LEGACY VPS deployment script (see deploy/legacy-vps/README.md)." >&2
    echo "Current procedure: docs/DEPLOYMENT_ARCHITECTURE.md" >&2
    exit 64
fi
# AgentNet — production deploy runbook.
# RUN ONLY AFTER staging is verified green via deploy/runbook-staging.sh
# AND PR #3 has been merged to main.

set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"
DATE_TAG="$(date +%F-%H%M)"

echo "=== AgentNet PROD deploy @ $DATE_TAG ==="
echo "branch: $(git rev-parse --abbrev-ref HEAD)"
echo "commit: $(git rev-parse --short HEAD)"

if [[ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]]; then
    echo "FATAL: prod must deploy from main. Currently on $(git rev-parse --abbrev-ref HEAD)."
    exit 1
fi

# 1. Sanity: env present + real secrets.
if [[ ! -f .env ]]; then
    echo "FATAL: .env missing"; exit 1
fi
for var in POSTGRES_PASSWORD REDIS_PASSWORD JWT_SECRET_KEY FLASK_SECRET_KEY; do
    val=$(grep -E "^${var}=" .env | head -1 | cut -d= -f2-)
    if [[ -z "$val" || "$val" == *CHANGE_ME* || "$val" == "your_"* ]]; then
        echo "FATAL: .env '$var' empty or placeholder"; exit 1
    fi
done
echo "✓ secrets look real"

# 2. Backup prod DB.
mkdir -p /opt/agentnet/backups
BACKUP_FILE="/opt/agentnet/backups/pre-prod-deploy-${DATE_TAG}.sql.gz"
echo "→ dumping postgres to $BACKUP_FILE …"
docker exec agentnet-postgres pg_dumpall -U "${POSTGRES_USER:-agentnet}" | gzip > "$BACKUP_FILE"
echo "✓ backup size: $(du -h "$BACKUP_FILE" | cut -f1)"

# 3. Tag rollback.
git tag -f "rollback-prod-${DATE_TAG}"
echo "✓ git tag rollback-prod-${DATE_TAG}"

# 4. Build + up prod stack.
echo "→ docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build"
docker compose -f docker-compose.yml -f docker-compose.prod.yml down
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

# 5. Wait for healthchecks.
echo "→ waiting for prod services to become healthy (max 240s)…"
deadline=$((SECONDS + 240))
while [[ $SECONDS -lt $deadline ]]; do
    bad=0
    for svc in agentnet-postgres agentnet-redis agentnet-registry agentnet-payment agentnet-simulation agentnet-dashboard; do
        status=$(docker inspect -f '{{.State.Health.Status}}' "$svc" 2>/dev/null || echo "missing")
        if [[ "$status" != "healthy" ]]; then
            bad=$((bad+1))
        fi
    done
    if [[ $bad -eq 0 ]]; then
        echo "✓ all prod services healthy"
        break
    fi
    sleep 5
done
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps

# 6. Localhost smoke probes (Caddy not yet reloaded).
echo "→ smoke: registry :8000"
curl -fsS --max-time 5 http://localhost:8000/healthz && echo
echo "→ smoke: payment :8001"
curl -fsS --max-time 5 http://localhost:8001/healthz && echo
echo "→ smoke: simulation :8002"
curl -fsS --max-time 5 http://localhost:8002/healthz && echo
echo "→ smoke: dashboard :8080"
curl -fsS --max-time 5 http://localhost:8080/healthz && echo

# 7. Reload Caddy with the new subdomain config.
if systemctl is-active --quiet caddy; then
    cp deploy/Caddyfile /etc/caddy/Caddyfile
    systemctl reload caddy
    echo "✓ caddy reloaded"
fi

echo
echo "=== PROD UP. Verify externally: ==="
echo "  curl -fsS https://agentnet.io.vn/healthz"
echo "  curl -fsS https://agentnet.io.vn/readyz"
echo "  curl -fsS https://agentnet.io.vn/.well-known/agent-card.json | jq .url"
echo "    # → must be https://agentnet.io.vn (not localhost)"
echo "  curl -fsS https://payment.agentnet.io.vn/healthz"
echo "  curl -fsS https://dashboard.agentnet.io.vn/healthz"
echo "  curl -sI  https://agentnet.io.vn/metaverse | head -3"
echo
echo "End-to-end money invariant smoke test:"
echo "  REGISTRY_URL=https://agentnet.io.vn \\"
echo "  PAYMENT_URL=https://agentnet.io.vn \\"
echo "  python examples/orchestrator_agent/orchestrator.py"
echo
echo "Rollback if needed:"
echo "  git -C $REPO checkout rollback-prod-${DATE_TAG}"
echo "  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build"
echo "  # If migration corrupted DB, restore:"
echo "  zcat $BACKUP_FILE | docker exec -i agentnet-postgres psql -U \${POSTGRES_USER:-agentnet} postgres"
