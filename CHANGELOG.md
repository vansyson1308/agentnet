# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Retired — Phase 3.1 pre-deploy boundary closure

- **One self-improvement control plane.** The worker's reflection loop
  (`REFLECTION_LOOP_*`, failed task → `ImprovementProposal`) and its
  `AGENT_BACKLOG.md` bridge (`PROPOSED` → `CONVERTED_TO_TASK` by file
  append) no longer run: they competed with the Autonomous Society Runtime,
  which owns the proposal lifecycle (world ingestion → Scout → Governor →
  Architect → Builder → QA → Security → promotion → fitness). Archived as
  `legacy/hermes/worker_reflection_loop.py` and `legacy/hermes/AGENT_BACKLOG.md`.
  Historical rows already in `CONVERTED_TO_TASK` because of the bridge are
  left untouched (see `docs/SOCIETY_RUNTIME.md`).
- **Synthetic activity retired from the active tree.** `agents/poll_agent.py`
  (one task every 30 s "to create activity"), `echo_agent.py` and
  `storyteller_agent.py` moved to `legacy/synthetic-agents/`; nothing starts
  them.
- **GitHub credential boundary.** The promotion controller's GitHub provider
  authenticates `git push` through a temporary `GIT_ASKPASS` helper (no token
  in the remote URL, argv or repository config) and obtains credentials from
  a `GitHubCredentialProvider` (`disabled` default, `static` for operators,
  `app` = GitHub App JWT → short-lived installation token with in-memory
  cache, refresh and single-flight). Implemented and tested with fakes; no
  App is configured.
- **Staging configuration contract** exposes the Phase-3 runtime settings
  (`docker-compose.staging.yml`); `tests/test_config_parity.py` keeps
  `SocietySettings`, `.env.example`, the staging compose file and
  `docs/DEPLOYMENT_ARCHITECTURE.md` in sync.

### Added — Agent goals and self-improvement loop

The biggest gap surfaced in `CURRENT_STATE.md` was: agents had capabilities,
tasks and reputation, but no overarching mission and no feedback path
when work failed. This wave closes that gap.

- **Goals** (`/v1/goals/*`, table `goals`): every agent can hold a primary
  mission text plus 0..N active goals with priority, success criteria, and
  a parent/child tree. Status state machine: `active <-> paused`,
  terminal `completed | failed | cancelled`.
- **Improvement Proposals** (`/v1/improvements/*`, table `improvement_proposals`):
  the spine of the self-improvement loop. Lifecycle
  `PROPOSED → UNDER_REVIEW → APPROVED → CONVERTED_TO_TASK → IMPLEMENTED`
  with a self-approval guard (proposer cannot approve own proposal).
- **Memory Items** (`/v1/memory/*`, table `memory_items`): durable
  society-/agent-scope lessons with importance scoring and JSONB tags
  (GIN-indexed for fast tag containment search).
- **Agent mission endpoints**: `GET/PATCH /v1/agents/{id}/mission`,
  `GET /v1/agents/{id}/goals`, `GET /v1/agents/{id}/lessons`.
- **Worker reflection loop**: `services/worker` now scans fresh
  failed/timeout/refunded `task_sessions` every
  `REFLECTION_LOOP_INTERVAL_SEC` (default 300s) and auto-generates one
  `ImprovementProposal` per task that doesn't already have one.
  Idempotent — never duplicates.
- **Flask dashboard pages** under `/goals`, `/improvements`, `/memory`,
  and `/agents/{id}/mission` for human-observable views.

#### Migrations

Three new sequential SQL files (`10-goals.sql`, `11-improvements.sql`,
`12-memory.sql`). All use `CREATE TABLE IF NOT EXISTS` /
`ADD COLUMN IF NOT EXISTS`, safe to re-apply on existing prod via the
new helper `services/registry/init-db/apply-pending.sh`.

#### Invariants preserved

CLAUDE.md's prime directive — never touch wallet/escrow correctness — is
preserved. The new tables are fully decoupled from the money path; the
`convert-to-task` action creates a `TaskSession` row but defers actual
escrow locking to the standard `/v1/tasks` pipeline. No new escrow code.

## [0.1.0] - 2024-01-01

### Added

#### Core Features
- User registration and authentication (JWT)
- Agent registration with capabilities
- Agent discovery and search
- Task session creation with escrow
- Wallet management (dual currency: credits + USDC)
- Transaction processing
- WebSocket support for real-time updates
- Distributed tracing with OpenTelemetry/Jaeger
- Background worker for auto-refund

#### Services
- Registry Service (port 8000)
- Payment Service (port 8001)
- Dashboard UI (port 8080)
- Worker Service

#### Documentation
- README.md with quick start guide
- API reference documentation
- Architecture documentation
- Deployment guide
- Contributing guidelines

### Technical

- PostgreSQL 15 database
- Redis 7 for caching and pub/sub
- SQLAlchemy 2.0 ORM
- FastAPI framework
- Docker Compose orchestration
- Unit tests (84+ tests)
- Integration tests

### Known Issues

- Agent verification flow requires manual endpoint implementation
- Some integration tests may be flaky
- Dashboard UI is basic

## [0.0.1] - 2023-12-01

### Added

- Initial project structure
- Basic database models
- Hello world FastAPI services

---

## Version History

| Version | Date | Status |
|---------|------|--------|
| 0.1.0 | 2024-01-01 | Released |
| 0.0.1 | 2023-12-01 | Initial release |

## Release Schedule

- **Patch releases**: As needed for bug fixes
- **Minor releases**: Monthly for new features
- **Major releases**: Breaking changes only

## Upcoming Features (Backlog)

- [ ] Agent verification/credentialing system
- [ ] Referral system
- [ ] Offer management
- [ ] Dispute resolution
- [ ] Enhanced dashboard with analytics
- [ ] SDK for JavaScript/TypeScript
- [ ] CLI tool
- [ ] Kubernetes deployment manifests

## Deprecation Notices

None at this time.

## Security Advisories

None at this time.
