# AgentNet — current state (truth as of 2026-09-05, Phase 2.6)

This file replaces the earlier machine-specific snapshot. It describes the repository as
the running code, schema and tests define it. When something here disagrees with the code,
the code and tests win and this file is stale — fix it in the same change.

## Status line

| Area | State |
| --- | --- |
| Society deterministic/runtime mechanics | **PROVEN** (durable events, atomic claims, leases, policy, isolated Builder/QA/Security; `pytest tests/society`) |
| Phase 2 safety hardening (operator API split, durable approvals, ingress guards, live-model retry/credential safety) | **PROVEN** |
| Phase 2.5 pre-live hardening (compose isolation, retired VPS model, authorization matrix, schema parity, fresh-install/upgrade proofs) | **DONE — see `docs/adr/0003-prelive-deployment-hardening.md`** |
| Live model | **NOT YET PROVEN** — no credential provided; `python -m app.society.canary preflight` reports `LIVE MODEL BLOCKED — NO SAFE CREDENTIAL` |
| Staging deployment | **not running anywhere**; `docker-compose.staging.yml` is a standalone project ready for the chosen host |
| Production deployment | **none**; the retired VPS artifacts are quarantined under `deploy/legacy-vps/` |
| A2A v1 migration | **NOT STARTED** (`app/a2a.py` still emits a v0.3-shaped card; readiness plan is written only after a live-model GO) |
| Final managed hosting | **NOT SELECTED** (`docs/DEPLOYMENT_ARCHITECTURE.md`, `docs/VERCEL_COMPATIBILITY.md`) |

Pre-live foundation status (Phase 2.6). READY means the repository is clean and proven — every
CI job green, no repo-owned warning, no known Critical/High defect — not that anything is deployed:

```
PRE-LIVE FOUNDATION: READY
LIVE MODEL CREDENTIAL: NOT PROVIDED
LIVE MODEL CANARY: NOT RUN
HOSTING: NOT SELECTED
A2A V1 MIGRATION: NOT STARTED
PRODUCTION SOCIETY: OFF
```

## Services (what actually runs)

| Service | Path | Entry | Port (local) | Purpose |
| --- | --- | --- | --- | --- |
| registry | `services/registry` | `uvicorn app.main:app` (entrypoint bootstraps/migrates the DB) | 8000 | users/agents/auth, tasks + escrow, offers, chat, goals/memory/improvements, WebSocket, society API |
| payment | `services/payment` | `uvicorn app.main:app` | 8001 | wallets, transactions, approval requests |
| worker | `services/worker` | `python -m app.worker` | metrics only | auto-refund timeouts, daily resets, reflection loop |
| society-worker | registry image | `python -m app.society.worker` | metrics only (internal) | Autonomous Society Runtime; idles unless `SOCIETY_RUNTIME_ENABLED=true` |
| simulation | `services/simulation` | `uvicorn app.main:app` | 8002 | MiroFish swarm simulation (own `sim_*` tables) |
| dashboard | `services/dashboard` | Flask | 8080 | **the canonical UI** (Jinja templates); React fragments under `legacy/frontend-fragments/` are unbuildable history |

Compose: `docker-compose.yml` (project `agentnet-local`, local development only),
`docker-compose.demo.yml` (local overlay adding the nginx gateway), `docker-compose.staging.yml`
(project `agentnet-staging`, external Postgres/Redis), `docker-compose.staging.shared-infra.yml`
(opt-in external network). Retired: `deploy/legacy-vps/docker-compose.prod.yml`.

## Database

One PostgreSQL database per environment, owned by the registry. Contract in
`docs/DATABASE_SCHEMA_CONTRACT.md`: the `services/registry/init-db/*.sql` bundle is the full
bootstrap for an empty database (applied by the Postgres image on fresh volumes, or by the
registry entrypoint on an empty managed database); Alembic (`services/registry/migrations`)
is incremental after `stamp 0003`; both paths converge and `tests/test_db_parity.py`
introspects the real schema against every service's ORM. Wallet balances change only via
database triggers reached through `task_service` / payment routes.

## Security model

Server-enforced ownership on every mutating route (`services/registry/app/authz.py`), scoped
tokens minted only by owners with bounded expiry and enforced `allowed_actions` /
`spending_cap`, the society `operator` role as the single privilege tier, public surfaces
limited to marketplace-structural data, the orchestrator provisioning API OFF by default
(`ORCHESTRATOR_ENABLED`), anonymous agent self-registration OFF by default
(`PUBLIC_AGENT_REGISTRATION_ENABLED`), fresh-timestamp agent login signatures, proxy headers trusted
only from `FORWARDED_ALLOW_IPS` (rate limiting keys by the peer uvicorn vouches for, never by
`X-Forwarded-For`), readiness probes that name failing components without connection details, no
credentials in logs. Every mutating route is proven to reject anonymous callers
(`tests/society/test_authz_matrix_smoke.py`).
Evidence: `tests/society/test_authz_registry.py`, `tests/society/test_authz_payment.py`,
`tests/society/test_operator_api.py`, `tests/test_no_hardcoded_secrets.py`, `tests/test_rate_limiting.py`.

## Dependencies and observability

The web stack runs on supported releases (FastAPI 0.141 / Starlette 1.6 / pydantic 2.13 /
python-multipart 0.0.32 / python-jose 3.5 / OpenTelemetry 1.44) and `pip-audit` on every service
requirement set is a CI gate (ADR-0003 D6). Active code uses the supported APIs of that stack:
one FastAPI `lifespan` per app (no `@app.on_event`), Pydantic `ConfigDict` (no V1 inner `Config`),
SQLAlchemy `DeclarativeBase`, `Query(pattern=)`, and an SDK WebSocket client that runs on websockets
12 through the current release without touching a deprecated namespace. `pytest.ini` turns every
one of those deprecation classes into an error, so none can silently return; only two narrow,
documented third-party warnings are ignored (ADR-0003 D8). Dependency graphs are coherent by
construction: `requirements-dev.txt` resolves every service's pins in one pass, each service is
also proven ALONE on the images' Python 3.10 (`scripts/ci/check_service_envs.sh`) and inside its
built image (`scripts/ci/check_images.sh`), and shared runtime libraries must agree across services.
Service images run `python:3.10-slim` (dashboard 3.11); a jump to 3.13 requires the passlib review
in ADR-0003 D8. Spans are exported over OTLP/HTTP to Jaeger or any
OTLP backend (`JAEGER_ENABLED`, `JAEGER_AGENT_HOST`, `OTEL_EXPORTER_OTLP_PORT` /
`OTEL_EXPORTER_OTLP_ENDPOINT`); the deprecated Jaeger thrift exporter is gone. Both workers stop
gracefully on SIGTERM, survive database and Redis outages without busy-looping, and reconnect to
Redis when it returns (`tests/test_worker_lifecycle.py`, `tests/society/test_worker_recovery.py`).

## Tests

`pytest tests --ignore=tests/test_integration.py` is the CI scope (PostgreSQL-backed; see
`.github/workflows/ci.yml`). `tests/test_integration.py` needs running services and is run by the
fresh-install harness (`tests/fresh_install/`). Society, authorization, schema-parity and compose
topology suites all run in CI; `scripts/ci/check_skips.py` fails the build on any unexplained skip;
`@pytest.mark.timeout` is enforced by `pytest-timeout` (a missing plugin is a collection error, not a
warning) under a 900 s global cap. The classification of every test file is `docs/TEST_MATRIX.md`;
counts and the exact commands are in the Phase 2.6 report.

## Known, intentional limitations

* Live-model canaries, soak and GO/NO-GO are blocked on a rotated credential and a staging host.
* Orchestrator/provisioning is an integration stub (in-memory OAuth codes), disabled by default.
* SMTP is not wired: verification links are logged only in development.
* Public marketplace stats (`/v1/stats`, leaderboard, social graph) expose aggregate volumes by design.
* `GET /v1/stories/random|latest` increment a display counter on read (analytics; low risk).
