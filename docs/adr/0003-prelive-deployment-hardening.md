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

### D8 — Warning policy, dependency coherence and the runtime contract (Phase 2.6)

The final pre-merge cleanup removed every repo-owned deprecation that the pinned stack reports and
made the test infrastructure truthful:

- **Deprecated APIs migrated.** Pydantic V1-style inner `class Config` (33 models, one `orm_mode`) →
  `model_config = ConfigDict(from_attributes=True)`; FastAPI `@app.on_event` → one `lifespan` per
  application (registry, payment, simulation) with the original ordering kept and a real bug fixed on
  the way — the old shutdown handlers awaited the synchronous `TracerProvider.shutdown()` and raised
  at every shutdown; `Query(regex=)` → `Query(pattern=)`; Starlette's renamed status constants;
  `declarative_base()` from `sqlalchemy.ext.declarative` → `class Base(DeclarativeBase)` in all four
  services (models keep their `Column()` attributes; metadata identity is asserted by
  `tests/test_sqlalchemy_base.py` and the real-Postgres parity suite); the SDK WebSocket client no
  longer imports `websockets.client`/`websockets.legacy` — `agentnet/ws.py` has ONE adapter for the
  classic (12.x) and current (≥ 13) client APIs and is tested end to end at both ends
  (`scripts/ci/check_sdk_envs.sh`).
- **`@pytest.mark.timeout` is real.** `pytest-timeout` is part of `requirements-dev.txt`, `pytest.ini`
  sets a 900 s global cap, and `tests/test_pytest_timeout_plugin.py` proves the plugin is loaded and
  kills a hanging test. Unknown marks are errors, so a missing plugin can never turn the marker into a
  silent no-op again.
- **Warning gate.** `pytest.ini` turns the migrated classes into errors (unknown marks, Pydantic
  V1 config, `MovedIn20Warning`, `on_event`, `regex=`, deprecated status constants, deprecated
  `websockets` namespaces, invalid escape sequences); the lint job additionally force-recompiles the
  tree with `-W error`. Third-party warnings are ignored one at a time, narrowly, and only these two:
  `passlib.utils` importing the stdlib `crypt` module (deprecated on 3.11, guarded, unused by bcrypt —
  see the runtime contract below) and Starlette 1.6's `anyio.abc.BlockingPortal` alias in its
  TestClient (starlette-side; no fix in the pinned release; test-only). GitHub-hosted runner notices
  (Redis `vm.overcommit_memory` in an ephemeral service container) are environment facts, not
  AgentNet warnings, and are left alone.
- **Dependency coherence.** CI used to install five requirement files one after another into one
  interpreter, so a later service could replace an earlier service's pin silently: the registry pins
  `websockets`, the simulation's unpinned LLM dependencies dragged it from 12 to 16, and the suite
  ran on a version the registry image never shipped. `requirements-dev.txt` is now the ONE test/dev
  set (it includes every service's requirements, so a conflicting pin fails the single resolver
  pass), CI runs `pip check`, the simulation's LLM dependencies are pinned, `bcrypt` is pinned next to
  `passlib` everywhere, and `scripts/ci/check_service_envs.sh` installs each service ALONE on the
  images' Python 3.10, `pip check`s it, import-smokes it (including a bcrypt hash/verify) and fails if
  a shared runtime library resolves to different versions across services. `scripts/ci/check_images.sh`
  repeats the import smoke inside the built images. The first isolated run immediately paid for
  itself: the dashboard image's requirement set listed an unused `httpx` and not the `requests` its
  API client imports — the image could not start alone, and no union-environment test could have
  noticed. It now pins `flask` and `requests`.
- **Runtime contract.** Service images run `python:3.10-slim` (dashboard 3.11); the CI suite runs on
  3.11 and the isolation job on 3.10. `passlib` 1.7.4 is unmaintained but its `crypt` import is
  guarded and bcrypt never needs it — `tests/test_runtime_contract.py` proves hashing with `crypt`
  absent (Python 3.13 semantics) and pins the interpreter versions, so a jump to 3.13 is a deliberate,
  reviewed change (replace passlib with `bcrypt` directly at that point; existing `$2b$` hashes stay
  valid). `bcrypt` stays at 4.0.0: passlib 1.7.4 reads `bcrypt.__about__` (gone in 4.1) and bcrypt 5
  changed the >72-byte probe it relies on.
- **`PYSEC-2026-1325` (ecdsa) revalidated.** Still no fixed release; `ecdsa` remains python-jose's
  fallback backend for EC signatures only, AgentNet signs and verifies with `HS256`
  (`JWT_ALGORITHM`, `jwt.decode(..., algorithms=[JWT_ALGORITHM])`), so the timing side channel has no
  reachable surface. Owner: registry/payment maintainers; review at the next python-jose or ecdsa
  release; nothing else is ignored.
- **GitHub Actions on Node 24.** `actions/checkout@v6`, `actions/setup-python@v6`,
  `actions/upload-artifact@v6`, `docker/setup-buildx-action@v4` (each `action.yml` declares
  `using: node24`; the inputs the workflow uses are unchanged). Permissions stay `contents: read`.
  The Postgres service healthchecks name the database they probe (`-d agentnet_test` / `-d postgres`),
  which ends the `FATAL: database "agentnet" does not exist` line every 5 s in the service logs.

## Consequences

- Operators must set managed-infra endpoints and secrets in the platform environment; staging refuses
  to render without them (fail fast by design).
- Anyone still on the old VPS must migrate: the legacy scripts no longer run.
- The next external inputs are exactly: hosting targets, managed database/cache, a rotated model
  credential, and the already-prepared live-model canary.
