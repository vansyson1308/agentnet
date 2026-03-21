#!/bin/bash
# ============================================================
# AgentNet Oracle Cloud Free Tier Setup Script
# ============================================================
# Designed for Ubuntu 22.04 on ARM Ampere A1 (4 OCPU, 24GB RAM)
#
# Usage:
#   ssh ubuntu@YOUR_VM_IP
#   curl -fsSL https://raw.githubusercontent.com/vansyson1308/agentnet/main/deploy/setup-oracle.sh | bash
#
# Or copy this script to the VM and run:
#   chmod +x setup-oracle.sh && ./setup-oracle.sh
# ============================================================

set -euo pipefail

echo "============================================"
echo "  AgentNet Setup — Oracle Cloud Free Tier"
echo "============================================"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[AgentNet]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARNING]${NC} $1"; }

# ============================================================
# 1. System updates
# ============================================================
log "Updating system packages..."
sudo apt-get update -y
sudo apt-get upgrade -y

# ============================================================
# 2. Install Docker
# ============================================================
if command -v docker &> /dev/null; then
    log "Docker already installed: $(docker --version)"
else
    log "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    log "Docker installed: $(docker --version)"
fi

# Ensure Docker Compose plugin is available
if docker compose version &> /dev/null; then
    log "Docker Compose available: $(docker compose version)"
else
    log "Installing Docker Compose plugin..."
    sudo apt-get install -y docker-compose-plugin
fi

# ============================================================
# 3. Install Caddy (reverse proxy + auto-SSL)
# ============================================================
if command -v caddy &> /dev/null; then
    log "Caddy already installed: $(caddy version)"
else
    log "Installing Caddy..."
    sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
    sudo apt-get update
    sudo apt-get install -y caddy
    log "Caddy installed: $(caddy version)"
fi

# ============================================================
# 4. Configure firewall (Oracle Cloud uses iptables by default)
# ============================================================
log "Configuring firewall (iptables)..."

# Oracle Cloud Ubuntu images use iptables, not UFW
# Allow SSH, HTTP, HTTPS — block everything else from outside
sudo iptables -I INPUT 1 -p tcp --dport 22 -j ACCEPT
sudo iptables -I INPUT 2 -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 3 -p tcp --dport 443 -j ACCEPT

# Block direct access to service ports from outside (only Caddy should access them)
sudo iptables -I INPUT 4 -p tcp --dport 5432 -j DROP 2>/dev/null || true  # PostgreSQL
sudo iptables -I INPUT 4 -p tcp --dport 6379 -j DROP 2>/dev/null || true  # Redis
sudo iptables -I INPUT 4 -p tcp --dport 8000 -j DROP 2>/dev/null || true  # Registry
sudo iptables -I INPUT 4 -p tcp --dport 8001 -j DROP 2>/dev/null || true  # Payment
sudo iptables -I INPUT 4 -p tcp --dport 8002 -j DROP 2>/dev/null || true  # Simulation
sudo iptables -I INPUT 4 -p tcp --dport 8080 -j DROP 2>/dev/null || true  # Dashboard

# Save iptables rules
sudo apt-get install -y iptables-persistent
sudo netfilter-persistent save

log "Firewall configured: ports 22, 80, 443 open"

# ============================================================
# 5. Clone repository
# ============================================================
APP_DIR="$HOME/agentnet"
if [ -d "$APP_DIR" ]; then
    log "Repository already exists at $APP_DIR, pulling latest..."
    cd "$APP_DIR"
    git pull origin main
else
    log "Cloning AgentNet repository..."
    git clone https://github.com/vansyson1308/agentnet.git "$APP_DIR"
    cd "$APP_DIR"
fi

# ============================================================
# 6. Generate .env with strong secrets
# ============================================================
if [ -f "$APP_DIR/.env" ]; then
    warn ".env already exists — skipping generation. Edit manually if needed."
else
    log "Generating .env with random secrets..."
    cp .env.example .env

    # Generate strong random passwords
    PG_PASS=$(openssl rand -hex 24)
    REDIS_PASS=$(openssl rand -hex 24)
    JWT_SECRET=$(openssl rand -hex 32)

    # Replace defaults with generated secrets
    sed -i "s/POSTGRES_PASSWORD=your_secure_password/POSTGRES_PASSWORD=$PG_PASS/" .env
    sed -i "s/REDIS_PASSWORD=your_redis_password/REDIS_PASSWORD=$REDIS_PASS/" .env
    sed -i "s/JWT_SECRET_KEY=your_jwt_secret_key/JWT_SECRET_KEY=$JWT_SECRET/" .env

    # Set production environment
    sed -i "s/ENVIRONMENT=development/ENVIRONMENT=production/" .env

    log ".env generated with strong secrets"
    warn "IMPORTANT: Edit .env to set CORS_ALLOWED_ORIGINS and REGISTRY_PUBLIC_URL"
    warn "  nano $APP_DIR/.env"
fi

# ============================================================
# 7. Configure Caddy
# ============================================================
log "Configuring Caddy reverse proxy..."
sudo cp "$APP_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile

# If no domain is set, use the public IP
PUBLIC_IP=$(curl -s ifconfig.me || curl -s icanhazip.com || echo "localhost")
log "Public IP detected: $PUBLIC_IP"

# For hackathon demo without domain, use IP with HTTP only
if [ -z "${DOMAIN:-}" ]; then
    warn "No DOMAIN set — Caddy will serve on http://$PUBLIC_IP (no SSL)"
    warn "To use SSL, set DOMAIN and re-run: DOMAIN=yourdomain.com ./setup-oracle.sh"
    sudo sed -i "s/{\$DOMAIN:localhost}/$PUBLIC_IP/" /etc/caddy/Caddyfile
fi

# ============================================================
# 8. Start services
# ============================================================
log "Building and starting AgentNet services..."
cd "$APP_DIR"

# Need to run as docker group member — use newgrp if needed
if groups | grep -q docker; then
    docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
else
    warn "Docker group not active yet. Running with sudo..."
    sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
fi

# ============================================================
# 9. Start Caddy
# ============================================================
log "Starting Caddy reverse proxy..."
sudo systemctl restart caddy
sudo systemctl enable caddy

# ============================================================
# 10. Setup daily database backup (cron)
# ============================================================
log "Setting up daily database backup..."
mkdir -p "$HOME/backups"

# Add cron job for daily backup at 3 AM
(crontab -l 2>/dev/null; echo "0 3 * * * docker exec agentnet-postgres pg_dump -U agentnet agentnet | gzip > $HOME/backups/agentnet-\$(date +\%Y\%m\%d).sql.gz && find $HOME/backups -name '*.sql.gz' -mtime +7 -delete") | sort -u | crontab -

log "Daily backup configured (3 AM, 7-day retention)"

# ============================================================
# 11. Verify
# ============================================================
log "Waiting for services to start (30s)..."
sleep 30

echo ""
echo "============================================"
echo "  Checking service health..."
echo "============================================"

check_service() {
    local name=$1
    local url=$2
    if curl -sf "$url" > /dev/null 2>&1; then
        echo -e "  ${GREEN}✓${NC} $name — OK"
    else
        echo -e "  ${YELLOW}✗${NC} $name — not ready yet (may need more time)"
    fi
}

check_service "Registry" "http://localhost:8000/health"
check_service "Payment" "http://localhost:8001/health"
check_service "Simulation" "http://localhost:8002/health"
check_service "Dashboard" "http://localhost:8080/health"

echo ""
echo "============================================"
echo "  AgentNet is running!"
echo "============================================"
echo ""
echo "  Public URL:  http://$PUBLIC_IP"
echo "  API:         http://$PUBLIC_IP/api/v1/agents/"
echo "  Dashboard:   http://$PUBLIC_IP"
echo ""
echo "  To check logs:"
echo "    docker compose logs -f registry"
echo "    docker compose logs -f payment"
echo ""
echo "  To update (after git push):"
echo "    cd ~/agentnet && git pull && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build"
echo ""
warn "Remember to update .env with:"
warn "  CORS_ALLOWED_ORIGINS=http://$PUBLIC_IP"
warn "  REGISTRY_PUBLIC_URL=http://$PUBLIC_IP"
echo ""
