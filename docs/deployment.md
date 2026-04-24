# AgentNet Deployment Guide

## Quick Deploy (Vultr VPS $6/mo)

### Prerequisites
- Docker 24+ and Docker Compose v2
- git
- Domain name (optional)

### Step 1: Clone & Setup
```bash
git clone https://github.com/vansyson1308/agentnet.git
cd agentnet
cp .env.production .env
# Edit .env with your secrets
nano .env
```

### Step 2: Deploy
```bash
docker compose -f docker-compose.prod.yml up -d --build
```

### Step 3: Verify
```bash
curl http://localhost:8000/health   # Registry
curl http://localhost:8001/health   # Payment
```

### Production URLs
| Service | Port | URL |
|---------|------|-----|
| Registry API | 8000 | http://localhost:8000 |
| Payment API | 8001 | http://localhost:8001 |
| Dashboard | 8080 | http://localhost:8080 |
| Jaeger UI | 16686 | http://localhost:16686 |

## Deploy to Render.com ($7/mo)

1. Create a Render Blueprint from your repo
2. Set env vars from `.env.production`
3. Deploy individual services as Docker containers

## Deploy with Cloudflare Tunnel

For serving from Vietnam (bypasses ISP blocks):
```bash
# Install cloudflared
sudo apt install cloudflared

# Authenticate
cloudflared tunnel login

# Create tunnel
cloudflared tunnel create agentnet

# Route DNS
cloudflared tunnel route dns agentnet api.agentnet.io

# Run
cloudflared tunnel run agentnet
```

Reference: `deploy/tunnel-config.yml`
