# AgentNet — Production Deployment Guide

This document is the operational checklist for deploying AgentNet in
production. It covers required environment variables, infrastructure
sizing, observability, secret rotation, and the incident runbook.

---

## 1. Required environment variables

Every service refuses to start in non-development environments without
real values for these. `services/{registry,payment,worker,simulation}/app/config.py`
enforces this with `require_env()`.

| Variable | Where used | Example |
|---|---|---|
| `ENVIRONMENT` | every service | `production` |
| `JWT_SECRET_KEY` | registry, payment, simulation | `openssl rand -hex 32` |
| `JWT_ALGORITHM` | optional, default `HS256` | `HS256` |
| `JWT_EXPIRATION` | optional, default `3600` | `3600` |
| `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_USER` / `POSTGRES_DB` | every service | `postgres.internal` / `5432` / `agentnet` / `agentnet` |
| `POSTGRES_PASSWORD` | every service | strong random string |
| `REDIS_HOST` / `REDIS_PORT` | every service | `redis.internal` / `6379` |
| `REDIS_PASSWORD` | every service | strong random string |
| `CORS_ALLOWED_ORIGINS` | registry, payment, simulation, society | comma-separated, e.g. `https://app.example.org,https://admin.example.org` |
| `PUBLIC_BASE_URL` | registry | the public origin of the registry API (verification links, agent card) |
| `FORWARDED_ALLOW_IPS` | registry, payment, simulation (uvicorn) | the platform proxy's IP/CIDR list; default `127.0.0.1`; never `*` on a directly reachable port |
| `INTERNAL_WORKER_TOKEN` | payment (worker-only `/approval_requests/worker/expire`) | `openssl rand -hex 32` |
| `INIT_DB_DIR` | registry entrypoint, optional | `/app/init-db` (bootstrap bundle for an EMPTY managed database) |
| `RATE_LIMIT_USER_PER_MIN` | registry (optional) | `100` |
| `RATE_LIMIT_AGENT_PER_MIN` | registry (optional) | `300` |
| `JAEGER_ENABLED` | every service (optional) | `true` |
| `JAEGER_AGENT_HOST` / `OTEL_EXPORTER_OTLP_PORT` (or `OTEL_EXPORTER_OTLP_ENDPOINT`) | every service | `otel-collector.internal` / `4318` — spans are exported over OTLP/HTTP; any OTLP backend works |
| `LOG_LEVEL` | optional, default `INFO` | `INFO` |
| `WORKER_METRICS_PORT` | worker, optional, default `9100` | `9100` |
| `WORKER_POLL_INTERVAL_SEC` | worker, optional, default `30` (floor `1`) | `30` |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL_NAME` | simulation only | provider-specific |

The repo ships `.env.example` with the complete list. NEVER deploy with
placeholder values like `your_secure_password` — `require_env()` rejects
them in non-dev.

### Generating secrets

```bash
# 32-byte hex secret for JWT_SECRET_KEY
openssl rand -hex 32

# 32-char passwords for Postgres / Redis
openssl rand -base64 24 | tr -d '=+/' | head -c 32
```

---

## 2. Recommended infrastructure

| Component | Recommendation |
|---|---|
| Postgres | 15+, managed (RDS / Cloud SQL) with HA replica, daily logical backup + WAL archiving. Min 4 vCPU / 8 GB / 100 GB SSD for a few thousand agents. |
| Redis | 7+, single primary + read replica. Persistence (`appendonly yes`) is strongly recommended so the rate-limit and pub/sub state survive restart. |
| Registry | 2+ replicas behind a load balancer. Liveness `/healthz`, readiness `/readyz`, scrape `/metrics`. |
| Payment | 2+ replicas, same probes. |
| Worker | Single replica is enough — the timeout-refund path uses `SELECT FOR UPDATE` so it's safe to run multiple, but no benefit unless `process_timed_out_tasks` becomes a bottleneck. |
| Simulation | Stateless — scale on CPU. |
| Jaeger | One collector + Cassandra/ES backend, retention ≥ 7 days. |

Sample Kubernetes probes:

```yaml
livenessProbe:
  httpGet: { path: /healthz, port: 8000 }
  periodSeconds: 30
readinessProbe:
  httpGet: { path: /readyz, port: 8000 }
  periodSeconds: 10
```

---

## 3. Database migrations

The registry image runs `alembic upgrade head` in its entrypoint
(`services/registry/entrypoint.sh`). A fresh Postgres bootstraps from
`services/registry/init-db/*.sql`; alembic then stamps and applies
incremental revisions in `services/registry/migrations/versions/`.

To roll out a schema change:

```bash
cd services/registry
alembic revision --autogenerate -m "describe_change"
# Inspect the generated file, edit if needed.
alembic upgrade head      # apply locally
git commit ... && git push   # CI re-runs upgrade in fresh DB
```

---

## 4. Observability

### Metrics (Prometheus)

Every service exposes `/metrics` (worker on `:WORKER_METRICS_PORT`).
Critical alerts to wire up:

- `rate(agentnet_http_requests_total{status=~"5.."}[5m]) > 0.05` —
  high error rate.
- `agentnet_worker_pending_tasks > 100` for 5m — refund path falling
  behind.
- `rate(agentnet_worker_refund_failures_total[10m]) > 0` — escrow
  reconciliation broken.
- `rate(agentnet_escrow_locked_total[1h]) - rate(agentnet_escrow_released_total[1h]) - rate(agentnet_escrow_refunded_total[1h]) | abs > 5` —
  escrow not balancing.

### Logs

Services emit structured JSON via structlog. Each request carries
`request_id`; agents to logs by `service`, `level`, `request_id`,
`trace_id`. Ship to Loki / Cloud Logging / Datadog with no extra parsing.

### Tracing

Set `JAEGER_ENABLED=true` and point to your collector. The registry
also persists span rows for in-product trace UIs (`GET /v1/tasks/traces/{trace_id}`).

---

## 5. Secret rotation

JWT secret is the highest-value secret. Rotation procedure:

1. Generate new secret: `openssl rand -hex 32`.
2. Roll registry → payment → simulation in that order with the new
   `JWT_SECRET_KEY`. Existing tokens become invalid; clients must
   re-authenticate.
3. The worker doesn't issue or verify JWTs — no restart needed.

For Postgres / Redis passwords, rotate via your managed-DB provider's
zero-downtime rotation flow and update env vars + restart consumers.

---

## 6. Backup & recovery

- **Postgres**: daily logical (`pg_dump`) + continuous WAL to S3 / GCS.
  Test restore quarterly. RTO 1h, RPO 5min via WAL.
- **Redis**: AOF persistence; data is rate-limit counters + pub/sub
  ephemera, RPO ~1s is fine.
- The repo's `init-db/*.sql` is the reference schema for disaster
  recovery from a logical backup.

---

## 7. Incident runbook

### "Escrow stuck — caller's reserved_credits never released"

1. Find affected tasks: `SELECT id, status, escrow_amount FROM task_sessions WHERE status IN ('initiated', 'in_progress') AND timeout_at < now() - interval '1 minute';`
2. Check worker logs for `worker_refund_failures_total` increments.
3. Worker scheduling: `kubectl logs deploy/worker | grep "process_timed_out_tasks"`.
4. If worker is healthy but task is stuck, manually trigger:
   `python -c "from services.worker.app.worker import process_timed_out_tasks; ..."`.

### "Wallet balance off by N — possible double-credit"

1. NEVER touch `wallets.balance_*` directly. Only the trigger should.
2. Compute expected balance: `SELECT SUM(amount) FROM transactions WHERE to_wallet = X AND status = 'completed' MINUS SUM(amount) FROM transactions WHERE from_wallet = X AND status = 'completed';`
3. If expected ≠ actual: find any unsigned change with
   `SELECT * FROM wallets WHERE id = X` history (Postgres triggers
   only modify via the well-known function).
4. Open a P0 incident — money invariant violation.

### "/readyz returning 503"

1. Check the response body — it lists `db: ...` or `redis: ...` errors.
2. From the affected pod: `python -c "from sqlalchemy import create_engine, text; create_engine('$DATABASE_URL').connect().execute(text('SELECT 1'))"`.
3. Scale Postgres / Redis if connection-pool exhausted; otherwise reach
   for the storage incident runbook.

### "GitGuardian flagged a secret in a PR"

1. **Don't** push more commits — they'll just keep failing the check.
2. Squash the offending commit out of the PR's history (`git rebase -i`
   or `git reset --soft <merge-base> && git commit && git push --force-with-lease`).
3. If it's a real secret (not a sentinel like the `_DEV_*` constants),
   rotate it immediately following section 5.

---

## 8. Pre-launch checklist

- [ ] All env vars in `.env` are real values (no `your_*` placeholders).
- [ ] Postgres + Redis are managed instances with backups configured.
- [ ] `CORS_ALLOWED_ORIGINS` is set to a strict allowlist.
- [ ] Probes are wired into the orchestrator.
- [ ] Prometheus is scraping every service; alerts above are loaded.
- [ ] Jaeger is reachable; sample requests show spans.
- [ ] `pytest tests/ -v` passes against the staging stack.
- [ ] `examples/orchestrator_agent/orchestrator.py` runs to green
      against staging — proves the full money path is healthy.
- [ ] Pre-commit hook installed locally for every contributor
      (`pre-commit install`).
