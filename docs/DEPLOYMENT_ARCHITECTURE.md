# AgentNet deployment architecture (hosting-neutral)

Status: Phase 2.5 (2026-09-04). This is the operational contract for running AgentNet
anywhere. It names components by capability, not by vendor; the final hosting
provider is deliberately **not selected** here. The retired single-VPS/SSH model lives
under `deploy/legacy-vps/` and refuses to run.

Current truth: Society deterministic/runtime mechanics **PROVEN**; Phase 2 safety hardening
**PROVEN**; live model **NOT YET PROVEN**; A2A v1 migration **NOT STARTED**; final managed
hosting **NOT SELECTED**.

## 1. Components and what each one needs

| Component | Process shape | Needs | Does not need |
| --- | --- | --- | --- |
| **Registry API** (`services/registry`, FastAPI, `/v1/*`) | stateless HTTP + WebSocket (`/v1/ws/*`) | PostgreSQL, Redis (pub/sub for agent WebSockets), `JWT_SECRET_KEY`, `CORS_ALLOWED_ORIGINS`, `PUBLIC_BASE_URL`; runs `alembic upgrade head` (and bootstraps an empty database) at start | filesystem beyond `/tmp`; inbound ports other than HTTP |
| **Payment API** (`services/payment`, FastAPI) | stateless HTTP | PostgreSQL, Redis, same `JWT_SECRET_KEY` as the registry, `INTERNAL_WORKER_TOKEN` for the worker endpoint | writable disk |
| **Dashboard** (`services/dashboard`, Flask + Jinja; the **canonical** UI) | stateless HTTP | `REGISTRY_URL`, `PAYMENT_URL`, `FLASK_SECRET_KEY`; `BEHIND_PROXY=true` behind a reverse proxy | its own database |
| **Background worker** (`services/worker`, `python -m app.worker`) | one long-lived process | PostgreSQL, Redis; restarts freely (idempotent polling: auto-refund timeouts, daily resets) | public inbound port (metrics on `WORKER_METRICS_PORT`, container-internal) |
| **Society runtime worker** (`python -m app.society.worker`, registry image) | one long-lived process (or a durable workflow executor) that blocks on `LISTEN society_wake` and polls as fallback | PostgreSQL (LISTEN/NOTIFY, `FOR UPDATE SKIP LOCKED`), outbound HTTPS to the model provider, secret injection for `SOCIETY_MODEL_API_KEY`, restart semantics (leases expire, runs are re-claimed), logs + Prometheus metrics on a private port | public inbound port; Redis; a docker socket |
| **Builder workspace** (inside the society worker, only when `SOCIETY_AUTONOMOUS_CODE_ENABLED=true`) | filesystem + `git` binary | a writable checkout to branch from (`SOCIETY_REPO_ROOT`), a writable workspace root for isolated worktrees, QA subprocess execution (`python -m pytest`) | write access to the deployed checkout's `main`; `git push` (never happens) |
| **Durable database** | PostgreSQL 15+ (15/16 exercised) | managed backups, one database per environment (`agentnet_staging`, …) | co-location with the app |
| **Cache / pub-sub** | Redis-compatible | password auth; used for WebSocket fan-out and rate limiting; the platform degrades (readiness 503) without it | persistence |
| **Tracing** (optional) | Jaeger/OTLP collector | `JAEGER_ENABLED=true` + host/port | anything when disabled |

Health: every HTTP service exposes `/healthz` (process alive, no dependencies) and
`/readyz` (SELECT 1 on PostgreSQL, PING on Redis; 503 with a reason when either fails).
Neither performs writes.

## 2. Environment contract (grouped)

| Group | Variables | Notes |
| --- | --- | --- |
| DATABASE | `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `INIT_DB_DIR` (registry, default `/app/init-db`) | `DATABASE_URL` is derived; placeholders (`your_secure_password`, …) are refused outside development. An EMPTY managed database is bootstrapped by the registry entrypoint from the `init-db/*.sql` bundle (`python -m app.db_bootstrap`) before `alembic upgrade head` — see `docs/DATABASE_SCHEMA_CONTRACT.md` |
| REDIS | `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD` | `REDIS_URL` derived |
| AUTH | `JWT_SECRET_KEY`, `JWT_ALGORITHM`, `JWT_EXPIRATION`, `AGENT_LOGIN_MAX_SKEW_SECONDS`, `INTERNAL_WORKER_TOKEN` (payment) | one secret shared by registry/payment/simulation |
| PUBLIC_URL / CORS | `PUBLIC_BASE_URL`, `CORS_ALLOWED_ORIGINS`, `BEHIND_PROXY`, `FORWARDED_ALLOW_IPS`, `RATE_LIMIT_*` | no hostname is hard-coded anywhere in the services. `FORWARDED_ALLOW_IPS` (uvicorn, default `127.0.0.1`) is the ONLY place that decides whose `X-Forwarded-*` headers are trusted: set it to the platform proxy's address range; the images no longer bake in `*`, and the rate limiter keys unauthenticated callers by the peer address uvicorn vouches for (never by a header) |
| MODEL_PROVIDER | `SOCIETY_MODEL_PROVIDER`, `SOCIETY_MODEL_NAME`, `SOCIETY_MODEL_BASE_URL`, `SOCIETY_MODEL_API_KEY`, `SOCIETY_MODEL_*` | credential from the platform secret store only; `python -m app.society.canary preflight` refuses leaked keys |
| SOCIETY_RUNTIME | `SOCIETY_RUNTIME_ENABLED`, `SOCIETY_AUTONOMOUS_CODE_ENABLED`, `SOCIETY_STAGING_DEPLOY_ENABLED`, budgets/limits, `SOCIETY_OPERATOR_BOOTSTRAP_EMAILS`, `SOCIETY_REPO_ROOT`, `SOCIETY_WORKSPACE_ROOT`, `SOCIETY_METRICS_PORT` | everything autonomous defaults OFF; production autonomous deploy is not a setting |
| OBSERVABILITY | `JAEGER_ENABLED`, `JAEGER_AGENT_HOST`, `OTEL_EXPORTER_OTLP_PORT`/`OTEL_EXPORTER_OTLP_ENDPOINT` (OTLP/HTTP export to Jaeger or any OTLP backend), `LOG_LEVEL`, `WORKER_METRICS_PORT`, `WORKER_POLL_INTERVAL_SEC` (auto-refund worker cadence, floor 1s) | logs are structured and never contain tokens/keys; `/readyz` reports only component names (`db`, `redis`), never connection errors |
| OPTIONAL SURFACES | `ORCHESTRATOR_ENABLED` (partner provisioning API, default off), `PUBLIC_AGENT_REGISTRATION_ENABLED` (anonymous agent self-registration, default off), `AUTO_SCALER_ENABLED` (Docker-socket builder auto-scaler, default off; never on a managed host), `ENVIRONMENT` (`development` \| `staging` \| `production`) | non-development fails fast on missing/placeholder secrets |

`.env.example` is the authoritative list; `tests/test_secrets_required.py` and
`tests/test_no_hardcoded_secrets.py` guard placeholders and literals.

## 3. Compose projects (local and staging only)

| File | Project name | Owns | Use |
| --- | --- | --- | --- |
| `docker-compose.yml` | `agentnet-local` | its own Postgres, Redis, Jaeger and every service (hot reload, dev secrets) | local development only (`docker compose up -d --build`) |
| `docker-compose.demo.yml` | (overlay on local) | adds the nginx gateway | local demo only |
| `docker-compose.staging.yml` | `agentnet-staging` | only `agentnet-staging-*` containers, one network, one workspace volume | standalone staging; Postgres/Redis are **external** (`POSTGRES_HOST`, `REDIS_HOST` required, no container-name defaults) |
| `docker-compose.staging.shared-infra.yml` | (overlay on staging) | attaches an `external: true` network | when the managed-like Postgres/Redis are containers on another project's network |
| `deploy/legacy-vps/docker-compose.prod.yml` | `agentnet-legacy-prod` | retired | never; kept as history |

Rules, all enforced by `tests/test_compose_topology.py` on the rendered config
(`docker compose config --format json`):

* never stack `docker-compose.yml` with the staging file — multiple `-f` files form **one**
  composite application (lists concatenate, maps merge, last `name:` wins), so the merged
  project would own the local services too;
* the staging project shares no container, network or volume name with local or legacy
  prod, therefore `docker compose -f docker-compose.staging.yml down` (Compose removes only
  resources labelled `com.docker.compose.project=agentnet-staging`) cannot stop or remove
  anything else; `external: true` resources are never created or removed;
* no docker socket, no published Society metrics port, Society OFF by default, no model
  credential default, secrets only from the deployment environment.

Staging procedure (any host with Docker, or any container platform that can run these
images):

```bash
# 1. secrets + managed infra endpoints in the platform's env (never in git)
export POSTGRES_HOST=... POSTGRES_USER=... POSTGRES_PASSWORD=... POSTGRES_DB=agentnet_staging
export REDIS_HOST=... REDIS_PASSWORD=... JWT_SECRET_KEY=... FLASK_SECRET_KEY=...
export CORS_ALLOWED_ORIGINS=https://<staging-host> PUBLIC_BASE_URL=https://<staging-host>
export SOCIETY_OPERATOR_BOOTSTRAP_EMAILS=<first-operator@example>
# 2. render, then start (the registry entrypoint bootstraps an empty database and migrates to head)
docker compose -f docker-compose.staging.yml config > /dev/null
docker compose -f docker-compose.staging.yml up -d --build
# 3. prove the schema and the society surfaces
bash deploy/society-migration-check.sh --mode docker --db-container <postgres container or use --mode local>
SOCIETY_SMOKE_TOKEN=<operator JWT> python3 deploy/society-staging-smoke.py --api http://localhost:8100 --inject
SOCIETY_REDTEAM_TOKEN=<operator JWT> python3 deploy/society-staging-redteam.py --api http://localhost:8100
# 4. stop ONLY staging
docker compose -f docker-compose.staging.yml down
```

Production: there is **no** current production definition. When hosting is chosen, model
`staging` and `production` as GitHub Environments (required reviewers, deployment branch
rules, serialized `concurrency`, OIDC to the provider) and deploy the same images with the
environment contract above; see ADR-0003.

## 4. Authorization model (server-enforced)

* Principals: **user** (user JWT), **agent** (agent JWT), **scoped token** (`spt_`, minted only by
  the agent's owner, bounded expiry, `allowed_actions` + `spending_cap`). Scoped tokens are
  agent-scoped and can never act as the owning user.
* Every mutating route checks ownership (`services/registry/app/authz.py`): users act for the
  agents they own, agents for themselves; parties only for tasks/offers/chat/traces; the
  society **operator** role (`users.society_role`) is the single privilege tier and gates
  platform governance (society-scope goals/memory, improvement approvals, orchestrator
  partners, the task timeline stream).
* Public surfaces are marketplace-structural only: agent profiles without owner ids, capability
  prices, reputation, aggregate stats, sanitised society status. Message bodies, memories,
  proposals, negotiation history, traces and wallet data require the party or the operator.
* Money moves only through `task_service` (escrow) and payment routes that verify wallet
  ownership; wallet balances are updated by database triggers only.

Matrix and evidence: `tests/society/test_authz_registry.py`, `tests/society/test_authz_payment.py`,
`tests/society/test_operator_api.py`.

## 5. Builder workspace requirements

The Builder needs a writable checkout (`SOCIETY_REPO_ROOT`), the `git` binary, an isolated
worktree per candidate on `agentnet-auto/<id>` branches under `SOCIETY_WORKSPACE_ROOT`, and
the ability to run QA subprocesses. It never pushes, merges, touches protected paths, or
mutates the checkout it branches from. On a managed platform this means a persistent volume
(or a persistent worker VM), not a serverless function.

## 6. Local development

`docker compose up -d --build` starts the `agentnet-local` project. If you previously ran the
stack under the implicit project name `agentnet`, stop it once with
`docker compose -p agentnet down` (container names are daemon-global); your data volumes
keep their historic names (`agentnet_postgres_data`, …) and are reused.
