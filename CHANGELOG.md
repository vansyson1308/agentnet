# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
