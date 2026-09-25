# AgentNet deployment architecture (hosting-neutral)

Status: Phase 2.5 (2026-09-04). This is the operational contract for running AgentNet
anywhere. It names components by capability, not by vendor; the final hosting
provider is deliberately **not selected** here. The retired single-VPS/SSH model lives
under `deploy/legacy-vps/` and refuses to run.

Current truth: Society deterministic/runtime mechanics **PROVEN**; Phase 2 safety hardening
**PROVEN**; live model **NOT YET PROVEN**; A2A v1 migration **NOT STARTED**; managed staging
hosting **Railway — DEPLOYED, `MANAGED STAGING — GREEN`** (Phase 4: project `AgentNet` / environment `staging`,
two consecutive full validations on `main` ae42d7a — `docs/RAILWAY_STAGING.md`, ADR-0006 D12; production: DARK since 2026-09-21, six services live from branch `production`, no public surface — `docs/PRODUCTION_DARK_PROOF.md`, ADR-0008).

## 1. Components and what each one needs

| Component | Process shape | Needs | Does not need |
| --- | --- | --- | --- |
| **Registry API** (`services/registry`, FastAPI, `/v1/*`) | stateless HTTP + WebSocket (`/v1/ws/*`) | PostgreSQL, Redis (pub/sub for agent WebSockets), `JWT_SECRET_KEY`, `CORS_ALLOWED_ORIGINS`, `PUBLIC_BASE_URL`; runs `alembic upgrade head` (and bootstraps an empty database) at start | filesystem beyond `/tmp`; inbound ports other than HTTP |
| **Payment API** (`services/payment`, FastAPI) | stateless HTTP | PostgreSQL, Redis, same `JWT_SECRET_KEY` as the registry, `INTERNAL_WORKER_TOKEN` for the worker endpoint | writable disk |
| **Dashboard** (`services/dashboard`, Flask + Jinja; the **canonical** UI) | stateless HTTP | `REGISTRY_URL` (the registry is the only backend it calls; `API_BASE_URL` is the legacy alias), `FLASK_SECRET_KEY`; `BEHIND_PROXY=true` behind a reverse proxy | its own database; a payment URL (`PAYMENT_URL` in the compose files is unused) |
| **Background worker** (`services/worker`, `python -m app.worker`) | one long-lived process | PostgreSQL, Redis, `REGISTRY_API_URL` (presence checks); restarts freely (idempotent polling: auto-refund timeouts, daily resets) | public inbound port (metrics on `WORKER_METRICS_PORT`, container-internal); the payment service or `INTERNAL_WORKER_TOKEN` (it never calls payment) |
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
| SOCIETY_RUNTIME | `SOCIETY_RUNTIME_ENABLED`, `SOCIETY_AUTONOMOUS_CODE_ENABLED`, `SOCIETY_STAGING_DEPLOY_ENABLED`, budgets/limits (`SOCIETY_DAILY_MODEL_BUDGET`, `SOCIETY_MAX_RUNS_PER_HOUR`, …), `SOCIETY_OPERATOR_BOOTSTRAP_EMAILS`, `SOCIETY_REPO_ROOT`, `SOCIETY_WORKSPACE_ROOT`, `SOCIETY_METRICS_PORT` | everything autonomous defaults OFF; production autonomous deploy is not a setting |
| SOCIETY_SELF_DEVELOPMENT (Phase 3) | `SOCIETY_MODEL_OUTPUT_FORMAT` (`auto`\|`json_object`\|`json_schema`), `SOCIETY_MODEL_CAPABILITY_PROFILE` (`generic`\|`deepseek`), `SOCIETY_MODEL_THINKING_MODE` (`auto`\|`disabled`\|`enabled`), `SOCIETY_MODEL_REASONING_EFFORT` (`auto`\|`none`\|`low`\|`medium`\|`high`\|`max`; DeepSeek: no `medium`) — Phase 4.1 provider request-capability layer (ADR-0007), `SOCIETY_MODEL_FAST_NAME`/`SOCIETY_MODEL_STRONG_NAME` (router tiers; provider names are config, never code), `SOCIETY_MAX_*_COST_USD`, engineering bounds (`SOCIETY_MAX_ENGINEERING_TURNS`, `SOCIETY_MAX_REPO_READS_*`, `SOCIETY_MAX_REPO_BYTES_PER_RUN`, `SOCIETY_MAX_SEARCH_RESULTS`), change budgets (`SOCIETY_MAX_AUTONOMOUS_CANDIDATES_PER_DAY`, `SOCIETY_MAX_RED_CANDIDATES_PER_DAY`, `SOCIETY_MAX_PROMOTIONS_PER_DAY`, `SOCIETY_MAX_OPEN_AUTONOMOUS_PRS`, `SOCIETY_MAX_FILES_PER_CANDIDATE`, `SOCIETY_MAX_DIFF_LINES`), `SOCIETY_PROMOTION_PROVIDER` (`disabled`\|`fake`\|`github`), `SOCIETY_AUTO_MERGE_ENABLED`, `SOCIETY_MAX_AUTONOMOUS_MERGES_PER_DAY`, `SOCIETY_GITHUB_REPOSITORY`/`_BASE_BRANCH`/`_API_URL`, `SOCIETY_DEPLOYMENT_PROVIDER` (`disabled`\|`fake`), `SOCIETY_FITNESS_TEST_TIMEOUT_SECONDS`, `SOCIETY_PROMOTION_LEASE_SECONDS`/`_MAX_ATTEMPTS`/`_POLL_INTERVAL_SECONDS`, `SOCIETY_REPO_ROOT`, `SOCIETY_WORKSPACE_ROOT`, `SOCIETY_METRICS_PORT` | promotion defaults to `disabled`, auto-merge to `false` (not overridable in the staging compose stack; when enabled elsewhere it is GREEN-only, never a draft PR, never under a freeze, and capped by `SOCIETY_MAX_AUTONOMOUS_MERGES_PER_DAY`); deployment defaults to `disabled` (`blocked_external`), production is refused. `docker-compose.staging.yml` exposes every name in this row for the society worker (`tests/test_config_parity.py` enforces it) |
| GITHUB_CREDENTIAL (Phase 3.1) | `SOCIETY_GITHUB_CREDENTIAL_PROVIDER` (`disabled`\|`static`\|`app`), `SOCIETY_GITHUB_APP_ID`, `SOCIETY_GITHUB_INSTALLATION_ID`, `SOCIETY_GITHUB_APP_PRIVATE_KEY_FILE` (a PATH), `SOCIETY_GITHUB_TOKEN_REFRESH_MARGIN_SECONDS` | read ONLY inside the Promotion Controller's credential provider (`society/github_credentials.py`). Secrets are never compose literals: the platform mounts the App private key as a file (or, only where a mount is impossible, injects `SOCIETY_GITHUB_APP_PRIVATE_KEY_PEM`) and, for the `static` provider, injects `SOCIETY_GITHUB_TOKEN` — into the controller process only, never the cognition worker, the API or the model context. Installation tokens are minted in memory (App JWT → 1 h token, refreshed before expiry, single-flight), never persisted, and reach `git push` only through a temporary `GIT_ASKPASS` helper (never a URL, argv or `.git/config`) |
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

Production: defined and deployed as of 2026-09-21 — Railway environment `production`,
deploying the protected `production` branch, never `main` (ADR-0008 D8). The Society
has no path to it: no society-worker service, no model or GitHub credential under any
name, and `production_deploy_enabled` is a hard `False`. When further hardening is
chosen, model `staging` and `production` as GitHub Environments (required reviewers, deployment branch
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
the ability to run QA subprocesses. It never pushes, merges, touches NEVER-write paths, or
mutates the checkout it branches from. On a managed platform this means a persistent volume
(or a persistent worker VM), not a serverless function. Pushing a candidate branch
(`agentnet-auto/<candidate-id>`) and opening a PR is the Promotion Controller's job through a
`PromotionProvider`; the future `github` provider needs the Society GitHub App identity and a
network path to the GitHub API from the controller process only (`docs/GITHUB_PROMOTION.md`).

## 6. Local development

`docker compose up -d --build` starts the `agentnet-local` project. If you previously ran the
stack under the implicit project name `agentnet`, stop it once with
`docker compose -p agentnet down` (container names are daemon-global); your data volumes
keep their historic names (`agentnet_postgres_data`, …) and are reused.

## 7. Managed staging on Railway (Phase 4)

The hosting-neutral contract above maps onto Railway without code forks; the decisions are ADR-0006 and the
operator runbook is `docs/RAILWAY_STAGING.md`. Summary of the mapping:

| Concern | Railway staging |
| --- | --- |
| Declaration | `.railway/railway.ts` (IaC DSL, staging-only guard, no secret values); config-as-code is deprecated and unused |
| Services | managed `postgres` + `redis` (private, reference variables only); `registry` and `dashboard` public on Railway-generated domains; `payment`, `worker`, `society-worker` private (`<service>.railway.internal`) |
| Schema owner | the registry pre-deploy command `sh -c 'SKIP_DB_BOOTSTRAP=false /app/entrypoint.sh true'`; every runtime container of the registry image starts with `SKIP_DB_BOOTSTRAP=true` and never migrates (local Compose: unset, unchanged) |
| Society workspace | one volume at `/workspace`; `services/registry/start-society-railway.sh` clones/reuses a credential-free public checkout, aligns it to `RAILWAY_GIT_COMMIT_SHA`, prunes stale worktree metadata only, then execs the worker (idle, `SOCIETY_RUNTIME_ENABLED=false`, metrics on 9101) |
| Client address | `TRUST_X_REAL_IP=true` on the registry (`app/proxy_headers.py`): `X-Real-IP` from Railway's edge; `X-Forwarded-For` never trusted; `FORWARDED_ALLOW_IPS` stays default — `*` would let callers mint rate-limit buckets |
| Secrets | shared variables `JWT_SECRET_KEY`, `FLASK_SECRET_KEY`, `INTERNAL_WORKER_TOKEN` (generated locally, stored only in Railway); no model key, no GitHub credential |
| Deploy gate | autodeploy from `main` + Wait for CI; healthchecks `/readyz` (registry, payment), `/healthz` (dashboard), `/metrics` (worker, society-worker); restart policy `Always` where the plan allows |
| State | `MANAGED STAGING — GREEN` (2026-09-18): project `AgentNet`, environment `staging`, two consecutive full validations from the in-environment `staging-validator` on `main` ae42d7a — `docs/RAILWAY_STAGING.md`. `PRODUCTION — DARK` (2026-09-21): environment `production` (`5e23ccb2`), six services live from branch `production` @ `60559d7f` (first gated release 2026-09-24), zero public domains, zero TCP proxies, no Society, no model or GitHub credential — `docs/PRODUCTION_DARK_PROOF.md`, ADR-0008. Validated: two consecutive clean in-environment runs (§10 of that document). Done by the owner and verified 2026-09-24: production *Wait for CI* ON for all four application services, and the `production` branch ruleset active (no bypass, six strict required checks). Email delivery live (Resend SMTP, `mail.agentnet.io.vn`, §12 of that document). The IaC (`.railway/production.ts`) declares the live environment exactly (ADR-0008 D13). Public since the Stage A cutover (2026-09-25): `https://api.agentnet.io.vn` → prod-registry and `https://dashboard.agentnet.io.vn` → prod-dashboard through ZoneDNS; the legacy VPS is retired (`docs/PRODUCTION_CUTOVER.md`). Delegated 2026-09-25: authoritative DNS moved from ZoneDNS to Cloudflare (zone `3a07bbdc…`, NS `aarav`/`leanna.ns.cloudflare.com`, active 07:57Z); api and dashboard serve through the Cloudflare edge. The apex `https://agentnet.io.vn` becomes the canonical UI on prod-dashboard, and `dashboard.*` a 301 compatibility host, once Railway verifies the apex ownership TXT (`docs/CLOUDFLARE_MIGRATION.md` §10.1) |
