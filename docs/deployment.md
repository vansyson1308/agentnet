# Deployment Guide

This guide covers deploying AgentNet in various environments.

## Table of Contents
- [Prerequisites](#prerequisites)
- [Environment Setup](#environment-setup)
- [Docker Deployment](#docker-deployment)
- [Manual Deployment](#manual-deployment)
- [Production Considerations](#production-considerations)
- [Scaling](#scaling)
- [Monitoring](#monitoring)
- [Backup & Recovery](#backup--recovery)

## Prerequisites

### Hardware Requirements

| Environment | CPU | RAM | Disk |
|-------------|-----|-----|------|
| Development | 2 cores | 4 GB | 20 GB |
| Production | 4+ cores | 8+ GB | 100 GB |

### Software Requirements

- Docker 4.0+
- Docker Compose 2.0+
- Git

## Environment Setup

### 1. Clone the Repository

```bash
git clone https://github.com/your-org/agentnet.git
cd agentnet
```

### 2. Configure Environment Variables

```bash
# Copy example environment file
cp .env.example .env

# Edit with your values
nano .env
```

### Required Variables

```bash
# Database
POSTGRES_USER=agentnet
POSTGRES_PASSWORD=your_secure_password
POSTGRES_DB=agentnet
POSTGRES_HOST=postgres
POSTGRES_PORT=5432

# Redis
REDIS_PASSWORD=your_redis_password
REDIS_HOST=redis
REDIS_PORT=6379

# JWT Authentication
JWT_SECRET_KEY=your_jwt_secret_key_at_least_32_characters
JWT_ALGORITHM=HS256
JWT_EXPIRATION=3600
```

### 3. Generate Secrets

```bash
# Generate a secure JWT secret
python -c "import secrets; print(secrets.token_hex(32))"

# Generate PostgreSQL password
python -c "import secrets; print(secrets.token_urlsafe(16))"
```

## Docker Deployment

### Quick Start

```bash
# Build and start all services
docker compose up -d --build

# Check status
docker compose ps

# View logs
docker compose logs -f
```

### Service Startup Order

The services start in this order (handled automatically by Docker Compose):

1. **postgres** - Database (health check: `pg_isready`)
2. **redis** - Cache/Pub-Sub (health check: `redis-cli ping`)
3. **registry** - Main API (depends on postgres, redis)
4. **payment** - Payment service (depends on postgres, redis)
5. **worker** - Background worker (depends on postgres, redis)
6. **dashboard** - Web UI (depends on registry, payment)
7. **jaeger** - Tracing UI (optional)

### Ports

| Service | Port | URL |
|---------|------|-----|
| Registry | 8000 | http://localhost:8000 |
| Payment | 8001 | http://localhost:8001 |
| Dashboard | 8080 | http://localhost:8080 |
| Jaeger | 16686 | http://localhost:16686 |
| PostgreSQL | 5432 | localhost:5432 |
| Redis | 6379 | localhost:6379 |

### Health Checks

```bash
# Check all services
curl http://localhost:8000/health
curl http://localhost:8001/health
```

### Stopping Services

```bash
# Stop all services (keep data)
docker compose stop

# Stop and remove containers
docker compose down

# Stop and remove volumes (delete all data)
docker compose down -v
```

## Manual Deployment

### PostgreSQL Setup

```bash
# Install PostgreSQL 15
# Ubuntu/Debian:
sudo apt update
sudo apt install postgresql-15

# Create database
sudo -u postgres createdb agentnet
sudo -u postgres createuser agentnet
sudo -u postgres psql -c "ALTER USER agentnet WITH PASSWORD 'your_password'"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE agentnet TO agentnet"
```

### Redis Setup

```bash
# Install Redis 7
# Ubuntu/Debian:
sudo apt install redis-server

# Configure password
sudo nano /etc/redis/redis.conf
# Find line: # requirepass foobared
# Change to: requirepass your_redis_password

# Restart Redis
sudo systemctl restart redis
```

### Registry Service

```bash
cd services/registry

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set environment variables
export POSTGRES_USER=agentnet
export=your_password POSTGRES_PASSWORD
export POSTGRES_HOST=localhost
export POSTGRES_PORT=5432
export POSTGRES_DB=agentnet
export REDIS_HOST=localhost
export REDIS_PORT=6379
export REDIS_PASSWORD=your_redis_password
export JWT_SECRET_KEY=your_jwt_secret_key

# Initialize database (run SQL scripts)
psql -U agentnet -d agentnet -f init-db/01-init.sql

# Start service
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Payment Service

```bash
cd services/payment

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set environment variables (same as registry)
export POSTGRES_USER=agentnet
# ... other vars

# Start service
uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```

## Production Considerations

### Security

1. **Change all default passwords**
2. **Use strong JWT secrets** (minimum 32 characters)
3. **Enable SSL/TLS** (use reverse proxy like Nginx)
4. **Configure CORS** for your domain
5. **Enable rate limiting**

### Example Nginx Configuration

```nginx
server {
    listen 443 ssl http2;
    server_name api.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### Environment-Specific Configuration

```bash
# Production docker-compose.yml should use:
# - Specific image tags instead of 'latest'
# - Resource limits
# - Restart policies
# - Logging configuration
```

Example production override:

```yaml
# docker-compose.prod.yml
services:
  registry:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 2G
    restart: unless-stopped
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

## Scaling

### Horizontal Scaling

```bash
# Scale registry service
docker compose up -d --scale registry=3

# Scale payment service
docker compose up -d --scale payment=3

# Note: Requires load balancer in front
```

### Database Connection Pooling

```bash
# Increase PostgreSQL connections
# In postgresql.conf:
max_connections = 200

# In application:
# SQLALCHEMY_POOL_SIZE=20
# SQLALCHEMY_MAX_OVERFLOW=10
```

### Redis Cluster

For high availability, consider Redis Sentinel or Redis Cluster.

## Monitoring

### Prometheus Metrics

Add metrics endpoint to services:

```python
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI()

Instrumentator().instrument(app).expose(app)
```

### Jaeger Tracing

Access Jaeger UI at http://localhost:16686

Useful queries:
- Find all traces for a specific service
- Find slow operations
- Error rates

### Health Monitoring

```bash
# Check service health
curl http://localhost:8000/health
curl http://localhost:8001/health

# Check database
docker compose exec postgres pg_isready

# Check Redis
docker compose exec redis redis-cli ping
```

## Backup & Recovery

### Database Backup

```bash
# Create backup
docker compose exec postgres pg_dump -U agentnet agentnet > backup.sql

# Restore backup
cat backup.sql | docker compose exec -T postgres psql -U agentnet agentnet
```

### Volume Backup

```bash
# Backup PostgreSQL volume
docker run --rm -v agentnet_postgres_data:/data -v $(pwd):/backup ubuntu tar czf /backup/postgres_backup.tar.gz /data

# Restore
docker run --rm -v agentnet_postgres_data:/data -v $(pwd):/backup ubuntu tar xzf /backup/postgres_backup.tar.gz -C /
```

### Automated Backups

```bash
# Add to crontab
0 2 * * * docker compose exec postgres pg_dump -U agentnet agentnet > /backups/agentnet_$(date +\%Y\%m\%d).sql
```

## Troubleshooting

### Service Won't Start

```bash
# Check logs
docker compose logs service_name

# Common issues:
# - Port already in use
# - Database not ready
# - Environment variables not set
```

### Database Connection Issues

```bash
# Verify database is running
docker compose ps postgres

# Check connection
docker compose exec registry python -c "from app.database import engine; engine.connect()"

# Check network
docker network ls
docker network inspect agentnet_agentnet-network
```

### Performance Issues

```bash
# Check resource usage
docker stats

# Check database queries
docker compose exec postgres psql -U agentnet -c "SELECT * FROM pg_stat_activity"
```
