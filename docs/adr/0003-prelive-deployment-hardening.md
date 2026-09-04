# ADR-0003 — Pre-live hardening: environment isolation, retired VPS model, hosting-neutral contract

- **Status:** Accepted (Phase 2.5, 2026-09-04)
- **Extends:** ADR-0001 (society runtime), ADR-0002 (Phase 2 hardening)
- **Boundary:** before any live model key, Vercel deployment, hosting choice, A2A migration, external
  onboarding, MCP federation or production Society activation.

## Context

Staging was deployed as `docker compose -f docker-compose.yml -f docker-compose.staging.yml` on a
single VPS reached over `root@` SSH with a checkout under `/opt/agentnet`. Multiple `-f` files form one
composite application, so that project also owned every local/dev service (`agentnet-registry`,
`agentnet-postgres`, …) and `down` could stop them. Runbooks named a fixed IP, an obsolete branch and a
PR number. The audit also found Critical authorization defects and ORM/schema drift (recorded in the
Phase 2.5 report and fixed alongside this ADR).

## References verified for this decision (2026-09-04)

Primary doc sites were egress-blocked; facts come from the canonical documentation source repositories
(`docker/docs`, `docker/compose` incl. Go source, `compose-spec/compose-spec`, `github/docs`,
`postgres/postgres` SGML, `sqlalchemy`, `alembic`), GitHub release pages, npm/PyPI metadata and the
docs bundled in the `workflow` npm package. Vercel numeric limits could only be read from search
excerpts and are flagged in `docs/VERCEL_COMPATIBILITY.md`.

| Area | Version / date | Facts used |
| --- | --- | --- |
| Docker Compose | v5.5.1 (2026-09-03); spec versionless, merge rules doc 2025-02-04; project-name doc 2026-02-24 | `-f` files merge into one application (lists concatenate; `environment`/`labels`/`volumes` merge by key; `command`/`entrypoint`/`healthcheck.test` replaced; paths relative to the first file); project name precedence `-p` > `COMPOSE_PROJECT_NAME` > last `name:` > first-file directory > cwd; `down` removes only resources labelled `com.docker.compose.project`; `external: true` networks/volumes are never created or removed; `container_name` is daemon-global; `--dry-run` is a global flag (needs the Engine API); `config` renders the merged, interpolated model |
| GitHub Actions | github/docs `main`, environments reference last changed 2026-07-16 | Environments with required reviewers (≤6, one approval, self-review prevention), wait timers (1–43,200 min), deployment branch/tag rules matched against `GITHUB_REF`, environment secrets released only after rules pass, `environment:` on a job creates deployment/status objects, `workflow_dispatch` typed inputs (≤25), `concurrency` groups with `cancel-in-progress`, OIDC subject `repo:<org>/<repo>:environment:<name>` |
| Vercel | `@vercel/python` 6.54.1; `workflow` 4.8.5 (2026-08-25) / 5.0.0-beta; `vercel` PyPI 0.10.0 (2026-08-12); WebSockets public beta 2026-06-22 (*snippet*); Workflows GA 2026-04-16 (*snippet*) | Functions are invocation-scoped; `waitUntil` extends only the current invocation; Cron is at-least-once (duplicates/misses possible); `/tmp` only writable; Python ASGI supported |
| PostgreSQL / SQLAlchemy / Alembic | PG 15 (2022-10-13) … PG 18 (2025-09-25), all minors 2026-08-13; SQLAlchemy 2.0.52; Alembic 1.19.1 | `FOR UPDATE SKIP LOCKED` queue semantics; NOTIFY delivered on commit, LISTEN session-scoped, payload < 8000 bytes; transactional DDL on PostgreSQL; `pool_pre_ping` recommended; `ALTER TABLE … ADD COLUMN IF NOT EXISTS` takes a brief ACCESS EXCLUSIVE lock |

## Decisions

### D1 — Every Compose file is its own explicitly named project; overlays never cross environments

`docker-compose.yml` carries `name: agentnet-local` (local development only, with pinned legacy
volume/network names so developer data survives the rename). `docker-compose.staging.yml` is a complete,
standalone project `agentnet-staging` that owns only `agentnet-staging-*` containers, its own network
and workspace volume, and addresses Postgres/Redis by env (`:?` required, no container-name defaults).
`docker-compose.staging.shared-infra.yml` is the only way to reach infra on another project's network
and does it through an `external: true` network. `tests/test_compose_topology.py` proves, on the
rendered configuration, that local and staging share no container/network/volume name, that staging
never renders without managed-infra env, and that no active file stacks base + overlay across
environments. Consequence: `docker compose -f docker-compose.staging.yml down` cannot touch local or
production resources because Compose scopes removal by project label and no names collide.

Rejected: relying on the directory name; `-p` alone (easy to forget); a filename migration to
`compose.local.yml` (no safety gain, breaks every documented command).

### D2 — The single-VPS/SSH deployment model is retired, not deleted

The prod overlay, both runbooks, the Oracle bootstrap, the Caddyfile and the tunnel config moved to
`deploy/legacy-vps/` (LEGACY) with a `LEGACY — DO NOT USE` banner; the scripts exit
64 unless `AGENTNET_ALLOW_LEGACY_VPS=I_UNDERSTAND_THIS_IS_RETIRED`; the prod overlay carries
`name: agentnet-legacy-prod`. No active runbook names an IP, `root@`, `/opt/agentnet`, a branch or a PR.
No production Compose definition is current: production arrives with the hosting decision.

### D3 — Hosting-neutral component contract instead of a vendor pick

`docs/DEPLOYMENT_ARCHITECTURE.md` describes each component by capability (stateless HTTP, long-lived
worker with LISTEN/NOTIFY, writable Builder workspace with git, managed Postgres/Redis) and groups the
environment contract (DATABASE, REDIS, AUTH, PUBLIC_URL/CORS, MODEL_PROVIDER, SOCIETY_RUNTIME,
OBSERVABILITY). Hostnames leave the code: `PUBLIC_BASE_URL` replaces the hard-coded verification URL, the
development CORS list drops production domains, the worker backlog path is configurable.
`docs/VERCEL_COMPATIBILITY.md` classifies components against the verified Vercel model; the Society
worker, WebSocket hub, Builder worktrees and QA subprocesses are **not suitable as-is**, so the future
shape is hybrid. No `vercel.json`, no provider secrets, no deployment workflow.

### D4 — Future deployments go through protected GitHub Environments, never personal shell access

When a provider is chosen, `staging`/`production` become GitHub Environments with required reviewers,
deployment-branch rules (`main` only for production), environment-scoped secrets, one `concurrency`
group per environment and OIDC to the provider. This ADR records the mechanism; the workflow is written
in the hosting phase.

### D5 — Authorization and schema parity are part of "deployment-safe"

The pre-live gate also requires: server-side ownership on every mutating route (see
`services/registry/app/authz.py`), scoped tokens minted only by owners with bounded expiry and enforced
`allowed_actions`/`spending_cap`, the orchestrator provisioning surface OFF by default, and a database
whose fresh-install and upgrade paths converge (`tests/test_db_parity.py`,
`docs/DATABASE_SCHEMA_CONTRACT.md`).

### D6 — The web stack is kept on supported, advisory-free releases; trace export is OTLP

`pip-audit` against the pinned requirement sets found Critical/High advisories in the shipped
FastAPI 0.104 / Starlette 0.27 / python-multipart 0.0.6 / python-jose 3.3 / python-dotenv 1.0 /
protobuf 4 stack (multipart DoS, JWT algorithm confusion, path/env handling). The services now pin
FastAPI 0.141.1 (Starlette 1.6), python-multipart 0.0.32, python-jose 3.5.0, python-dotenv 1.2.3,
httpx 0.28.1, uvicorn 0.52.4, pydantic 2.13.5 and OpenTelemetry 1.44 / 0.65b0. The deprecated
Jaeger **thrift** exporter cannot run on a current SDK, so every service exports spans over
**OTLP/HTTP** (`opentelemetry-exporter-otlp-proto-http`) — Jaeger all-in-one already accepts OTLP,
and so does every managed tracing backend, which removes the last Jaeger-specific dependency.
`JAEGER_ENABLED` keeps its name as the export toggle; `JAEGER_AGENT_PORT` is retired in favour of
`OTEL_EXPORTER_OTLP_PORT` / `OTEL_EXPORTER_OTLP_ENDPOINT`. The unused `aioredis` and the grpc Jaeger
exporter (which pinned protobuf < 5) are dropped. One advisory is consciously accepted:
`PYSEC-2026-1325` (ecdsa timing side channel) has no fixed release; `ecdsa` is only python-jose's
fallback backend and AgentNet signs JWTs with HS256, so CI ignores exactly that id and nothing else.
CI runs `pip-audit` on every service requirement set as a hard gate.

### D7 — Proxy headers are trusted only from an operator-declared proxy

The images no longer start uvicorn with `--forwarded-allow-ips '*'`. Trust for `X-Forwarded-*` comes
from `FORWARDED_ALLOW_IPS` (uvicorn's own variable, default loopback), set per deployment to the
platform proxy's address range. The registry rate limiter keys unauthenticated callers by the peer
address uvicorn vouches for and never parses `X-Forwarded-For` itself; a caller-controlled header can
therefore no longer mint a fresh rate-limit bucket per request.

## Consequences

- Operators must set managed-infra endpoints and secrets in the platform environment; staging refuses
  to render without them (fail fast by design).
- Anyone still on the old VPS must migrate: the legacy scripts no longer run.
- The next external inputs are exactly: hosting targets, managed database/cache, a rotated model
  credential, and the already-prepared live-model canary.
