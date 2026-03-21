#!/bin/bash
# ============================================================
# AgentNet Demo — Cloudflare Tunnel + Docker Compose
# ============================================================
# Starts all services locally and exposes via Cloudflare Tunnel
# Result: a public HTTPS URL for hackathon demo
#
# Prerequisites:
#   - Docker Desktop running
#   - cloudflared installed (winget install cloudflare.cloudflared)
#
# Usage:
#   ./deploy/start-demo.sh
# ============================================================

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log() { echo -e "${GREEN}[AgentNet]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
info() { echo -e "${CYAN}[INFO]${NC} $1"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# ============================================================
# 1. Check prerequisites
# ============================================================
log "Checking prerequisites..."

if ! command -v docker &> /dev/null; then
    echo "ERROR: Docker not found. Please install Docker Desktop."
    exit 1
fi

if ! docker info &> /dev/null 2>&1; then
    echo "ERROR: Docker is not running. Please start Docker Desktop."
    exit 1
fi

if ! command -v cloudflared &> /dev/null; then
    warn "cloudflared not found. Installing..."
    if command -v winget &> /dev/null; then
        winget install cloudflare.cloudflared
    elif command -v brew &> /dev/null; then
        brew install cloudflared
    else
        echo "Please install cloudflared manually:"
        echo "  https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
        exit 1
    fi
fi

log "All prerequisites OK"

# ============================================================
# 2. Create .env if not exists
# ============================================================
if [ ! -f .env ]; then
    log "Creating .env from .env.example..."
    cp .env.example .env
    warn "Edit .env to configure LLM_API_KEY if you want simulation to work"
fi

# ============================================================
# 3. Start Docker Compose
# ============================================================
log "Starting AgentNet services (demo mode)..."
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build

log "Waiting for services to be ready (30s)..."
sleep 30

# Check health
for service in "registry:8000" "payment:8001" "simulation:8002"; do
    name="${service%%:*}"
    port="${service##*:}"
    if curl -sf "http://localhost:$port/health" > /dev/null 2>&1; then
        info "$name (port $port) — OK"
    else
        warn "$name (port $port) — not ready yet"
    fi
done

# ============================================================
# 4. Start Cloudflare Tunnel
# ============================================================
echo ""
log "Starting Cloudflare Tunnel..."
echo ""
echo "============================================"
echo "  AgentNet Demo is starting!"
echo "============================================"
echo ""
echo "  Local services (via Nginx gateway on port 80):"
echo "    Gateway:     http://localhost (all APIs)"
echo "    Registry:    http://localhost:8000"
echo "    Payment:     http://localhost:8001"
echo "    Simulation:  http://localhost:8002"
echo "    Dashboard:   http://localhost:8080"
echo ""
echo "  Cloudflare Tunnel will generate a public HTTPS URL below."
echo "  Use that URL as your Demo Link for the hackathon."
echo ""
echo "  ALL APIs accessible via one URL:"
echo "    /api/v1/agents/        (registry)"
echo "    /api/v1/wallets/       (payment)"
echo "    /api/v1/simulations/   (simulation)"
echo "    /health                (health check)"
echo "    /                      (dashboard)"
echo ""
echo "  Press Ctrl+C to stop the tunnel."
echo ""
echo "============================================"
echo ""

# Start tunnel — exposes Nginx gateway (port 80) which routes to all services
# The tunnel URL will be printed by cloudflared
cloudflared tunnel --url http://localhost:80
