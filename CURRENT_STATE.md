# AgentNet — current state (truth as of 2026-09-19, Phase 5 closure)

This file replaces the earlier machine-specific snapshot. It describes the repository as
the running code, schema and tests define it. When something here disagrees with the code,
the code and tests win and this file is stale — fix it in the same change.

## Status line

| Area | State |
| --- | --- |
| Society deterministic/runtime mechanics | **PROVEN** (durable events, atomic claims, leases, policy, isolated Builder/QA/Security; `pytest tests/society`) |
| Phase 2 safety hardening (operator API split, durable approvals, ingress guards, live-model retry/credential safety) | **PROVEN** |
| Phase 2.5 pre-live hardening (compose isolation, retired VPS model, authorization matrix, schema parity, fresh-install/upgrade proofs) | **DONE — see `docs/adr/0003-prelive-deployment-hardening.md`** |
| Phase 3 self-development mechanics (read-only repo intelligence, bounded engineering loop, trusted risk tiers, non-LLM Promotion Controller, offline fitness engine, memory provenance, model routing + cost governor, DeepSeek-compatible output negotiation) | **PROVEN with deterministic fakes** — `pytest tests/society`, `examples/demo_autonomous_society.py --story code`; design in `docs/SELF_DEVELOPMENT.md`, `docs/GITHUB_PROMOTION.md`, `docs/FITNESS_EVALUATION.md`, ADR-0004 |
| Real source-code candidate | **PROVEN DETERMINISTICALLY** — the scripted fleet fixes a planted defect in an isolated fixture application, adds a test, passes QA + Security, gets a shadow PR and a fitness PASS. Scripted coding is runtime proof, not model-quality evidence |
| Society GitHub App / real PR promotion | **IMPLEMENTED, NOT CONFIGURED** — `GitHubCredentialProvider` (`disabled` default, `static`, `app` = App JWT → short-lived installation token, in-memory cache/refresh/single-flight) and `GIT_ASKPASS`-based `git push` (no token in URL/argv/config); inert until an owner registers the App and mounts its private key into the controller only (`docs/GITHUB_PROMOTION.md`, ADR-0005). No real promotion has run |
| One self-improvement control plane | **DONE (Phase 3.1)** — the worker's reflection loop and `AGENT_BACKLOG.md` bridge are archived under `legacy/hermes/`; synthetic poll/echo/storyteller agents under `legacy/synthetic-agents/`; `tests/society/test_single_control_plane.py` |
| `main` ruleset | **ACTIVE** — configured by the owner (2026-09-19) from `deploy/github/main-ruleset.json`: pull request required, review threads resolved, the six CI jobs required and strict, no bypass actors; enforced on the Phase 4.1 PR |
| Live model | **LIVE — OPERATIONAL; autonomous promotion NOT yet proven (Phase 6, 2026-09-20)** — the Society runs on real DeepSeek against Railway staging with `SOCIETY_RUNTIME_ENABLED=true`: 175 completed live runs, 0 non-live, 0 DEAD in the final window, $0.082 spent. Proven live: the full engineering chain from one world event to a real `CodeCandidate` (causation depth 7); multi-agent operation on a REAL `task.failed`; both approval lifecycles; operator memory refutation with an append-only audit row; the GitHub App credential minting a correctly scoped installation token (**GITHUB APP READY** ×4, installation scoped to exactly this repository, Actions secrets refused 403); the promotion controller **refusing** a `REQUEST_PR_PROMOTION` for a candidate that was not READY; the anti-busywork guard rejecting a no-op candidate before QA; `SOCIETY RED-TEAM: ALL DEFENDED` twice against live cognition. **Not** proven live: any candidate producing a real diff, and therefore no promotion record exists — see `docs/SOCIETY_LIVE_PROOF.md` §4 |
| Staging deployment | **Railway managed staging — GREEN** (2026-09-18, `main` ae42d7a): project `AgentNet`, environment `staging`, Postgres + Redis (private), registry + dashboard on Railway-generated domains, payment / worker / society-worker private, one `society-workspace` volume, registry pre-deploy as the only migration owner (`0010_self_development`, fleet seed), `TRUST_X_REAL_IP` spoof test PASS at the live edge, Society flags OFF, scripted model only. Two consecutive full validations by the in-environment `staging-validator` (`deploy/railway/validate_staging.py`, 26 checks each, distinct operators) plus restart / persistence / rollback / secret-leak / resource proofs — `docs/RAILWAY_STAGING.md` §20. Open owner action: *Wait for CI* on each service (the flag does not persist through the connector). `docker-compose.staging.yml` remains the Compose alternative |
| Production deployment | **PUBLIC on api + dashboard through Cloudflare; apex pending Railway's ownership check** (2026-09-25): Railway environment `production` (`5e23ccb2`), six services from branch `production` @ `60559d7f` (first gated release 2026-09-24, approved `main` `adbe0a53`, tree-identical; frozen since). `https://api.agentnet.io.vn` → prod-registry and `https://dashboard.agentnet.io.vn` → prod-dashboard serve through ZoneDNS since the Stage A cutover (`docs/PRODUCTION_CUTOVER.md`): edge smoke 34/36, the two failures being the apex's missing HTTPS; public signup → Resend email → verify → login proven through `https://api.agentnet.io.vn` (11/11). The legacy VPS is retired. The owner delegated `agentnet.io.vn` to Cloudflare (zone active 2026-09-25T07:57Z); api/dashboard, signup and email re-proven through the edge. The apex becomes the canonical UI once Railway verifies its ownership TXT (`docs/CLOUDFLARE_MIGRATION.md` §10.1). Zero TCP proxies, no Railway-generated domain, payment/worker/Postgres/Redis private. `.railway/production.ts` declares three domains and the final apex CORS origin (merged only after the post-delegation CORS change; ADR-0008 D13) |
| A2A v1 migration | **NOT STARTED** (`app/a2a.py` still emits a v0.3-shaped card; readiness plan is written only after a live-model GO) |
| Final managed hosting | **Railway** — staging DEPLOYED AND VALIDATED (`docs/RAILWAY_STAGING.md`, ADR-0006 D12); production PUBLIC on api/dashboard through Cloudflare, apex pending Railway's ownership check (`docs/PRODUCTION_CUTOVER.md`, `docs/CLOUDFLARE_MIGRATION.md`, ADR-0008) |

Self-development status (Phase 3). PROVEN means the mechanics are exercised by deterministic
tests and the demo — not that any model, GitHub App or host has been connected:

```
SELF-DEVELOPMENT MECHANICS: PROVEN
REAL SOURCE CODE CANDIDATE: PROVEN DETERMINISTICALLY
SHADOW PR PROMOTION: PROVEN
OFFLINE FITNESS: PROVEN
LIVE MODEL: PROVEN LIVE (deepseek-flash, 2026-09-19) — 57 completed runs, 0 DEAD, 0 retries
REAL SOCIETY GITHUB APP: NOT YET CONFIGURED
HOSTING: RAILWAY STAGING DEPLOYED (project AgentNet / environment staging)
MANAGED STAGING: GREEN (two consecutive full validations, distinct actors, on main 23b73f7)
STAGING OPERATOR: CREATED
SECRET LEAK CHECK: PASS
CHAIN OF THOUGHT STORED: NO
LIVE SOCIETY: AUTONOMOUS EVOLUTION LIVE (runtime ON, autonomous code ON, promotion github, GREEN auto-merge ON in staging, deploy OFF) — as of 2026-09-20T22:02Z
AUTONOMOUS CODE CANDIDATE (LIVE): PROVEN — candidate b8cee13c, real diff, 1 file/+75 (docs/SOCIETY_LIVE_PROOF.md §9)
AUTONOMOUS PROMOTION (LIVE): PROVEN — promotion c6a8a473 -> PR #30 -> merged by the Society App as 34ef7f6, human_approvals []
SOCIETY RED-TEAM (LIVE, RUNTIME ON): ALL DEFENDED
DEEPSEEK KEY PRESENT: YES (Railway society-worker only; never read, printed or copied)
SOCIETY GITHUB SECRET PRESENT: NO
WAIT FOR CI ON RAILWAY: ACTIVE (verified holding deployments through Phase 5)
PRODUCTION DEPLOYMENT: PUBLIC ON api + dashboard (branch `production` @ 60559d7f, frozen;
  released from main adbe0a53 through the gate)
PRODUCTION IaC: .railway/production.ts declares api -> prod-registry:8000, agentnet.io.vn +
  dashboard.agentnet.io.vn -> prod-dashboard:8080, and the FINAL CORS origin https://agentnet.io.vn;
  merged only after the post-delegation CORS change (offline plan today: 1 change = that CORS value)
PRODUCTION CUSTOM DOMAINS: api (VALID), dashboard (VALID), apex agentnet.io.vn (CREATED 2026-09-25;
  CNAME + ownership TXT public through Cloudflare; Railway ownership check PENDING -> apex 404)
CLOUDFLARE ZONE: ACTIVE since 2026-09-25T07:57Z (3a07bbdc..., Free, NS aarav/leanna.ns.cloudflare.com),
  SSL Full, Universal SSL active, BIC off for api only, dashboard->apex 301 rule prepared (disabled), DNSSEC off
EMAIL DELIVERY: smtp via Resend on mail.agentnet.io.vn (domain VERIFIED, send-only
  domain-scoped key, smtp.resend.com:2465 — the platform drops 465/587)
EMAIL/ACCOUNT FLOW: PROVEN LIVE 2026-09-24 — register -> AgentNet's own message delivered
  -> verify -> replay rejected -> login -> authenticated reads (11/11, exit 0)
PUBLIC HUMAN SIGNUP BACKEND: READY
PUBLIC INTERNET CLICKABILITY: LIVE through https://api.agentnet.io.vn behind Cloudflare (2026-09-25,
  11/11, real inbox, real link); the apex https://agentnet.io.vn serves HTTPS at the edge but answers
  404 until Railway verifies its ownership TXT
EMAIL SECRET LEAK CHECK: PASS (complete registry log, not a sample)
DNS CHANGED: YES — authoritative DNS delegated to Cloudflare by the owner (2026-09-25); ZoneDNS retired
  from the delegation, its zone untouched as the rollback target
A2A V1: NOT STARTED
PRODUCTION SOCIETY: OFF
LEGACY FILE BACKLOG: RETIRED FROM ACTIVE RUNTIME
SYNTHETIC POLL ACTIVITY: LEGACY/DEMO ONLY
GITHUB APP AUTH: IMPLEMENTED, NOT CONFIGURED
REAL GITHUB PROMOTION: NOT RUN
MAIN RULESET: ACTIVE (owner-configured 2026-09-19; required checks = the six CI jobs, thread resolution, no bypass)
DEEPSEEK KEY: PROVIDED TO RAILWAY SOCIETY-WORKER ONLY
```

Maturity levels (`docs/SELF_DEVELOPMENT.md`): levels 0–3 are **proven live** as of 2026-09-20 — a real
signal became a real candidate, a real PR opened by the Society GitHub App, and a GREEN change merged to
`main` by the App with no human approval (docs/SOCIETY_LIVE_PROOF.md §9). Level 4 (staging-live evaluation)
remains interface-only: `SOCIETY_DEPLOYMENT_PROVIDER=disabled`, requests end `blocked_external`. Level 5
(production) is not a setting and is refused by `config.py`.

## Services (what actually runs)

| Service | Path | Entry | Port (local) | Purpose |
| --- | --- | --- | --- | --- |
| registry | `services/registry` | `uvicorn app.main:app` (entrypoint bootstraps/migrates the DB) | 8000 | users/agents/auth, tasks + escrow, offers, chat, goals/memory/improvements, WebSocket, society API |
| payment | `services/payment` | `uvicorn app.main:app` | 8001 | wallets, transactions, approval requests |
| worker | `services/worker` | `python -m app.worker` | metrics only | auto-refund timeouts, daily resets, reputation, offline detection, card crawling, simulation timeouts — no self-improvement logic (the legacy reflection/backlog bridge was retired in Phase 3.1) |
| society-worker | registry image | `python -m app.society.worker` (on Railway: `sh /app/start-society-railway.sh` bootstraps the volume checkout first) | metrics only (internal) | Autonomous Society Runtime (cognition + Promotion Controller + fitness engine + telemetry producers); idles unless `SOCIETY_RUNTIME_ENABLED=true`; never holds a GitHub or deploy credential in the cognition path |
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

* Railway staging (ADR-0006 D12): *Wait for CI* was switched on by the owner in the dashboard on 2026-09-19
  (`checkSuites: true` on registry, payment, worker, dashboard and society-worker; the flag does not persist
  through the connector); the dashboard runs Flask's development server behind Railway's edge (a WSGI server
  is a follow-up); stale dashboard template links render as inert `#` anchors; the `kill 1` crash test and the
  volume marker file of the runbook need `railway ssh` and were not run — the restart policy (`ALWAYS`) and the
  bootstrap's `reusing persistent checkout` log lines are the evidence instead.
* Live-model canaries, soak and GO/NO-GO are blocked on a rotated credential and a staging host. The DeepSeek
  key now exists only in the Railway `society-worker` (Phase 4.1: preflight probes only, no canary run); provider
  compatibility is proven only against a fake transport (`tests/society/test_deepseek_contract.py`).
* Promotion runs against the real GitHub provider on the staging society-worker, and GREEN autonomous merge
  is ON there (repository default in `.env.example` and `docker-compose.staging.yml` stays `false`). No intent
  can enable it, raise `SOCIETY_MAX_AUTONOMOUS_MERGES_PER_DAY` (1), or merge anything that is not GREEN,
  not a non-draft PR, or under a merge freeze — the controller persists its verdict and the provider
  independently re-reads and re-checks it. `main` is protected by the owner-configured ruleset
  (`deploy/github/main-ruleset.json`); every change goes through a pull request with required checks green.
* Fitness is `offline` only; staging-live evaluation, rollback execution and any deployment need a
  `DeploymentProvider` that is `disabled` (requests end `blocked_external`).
* Orchestrator/provisioning is an integration stub (in-memory OAuth codes), disabled by default.
* SMTP is not wired: verification links are logged only in development.
* Public marketplace stats (`/v1/stats`, leaderboard, social graph) expose aggregate volumes by design.
* `GET /v1/stories/random|latest` increment a display counter on read (analytics; low risk).
