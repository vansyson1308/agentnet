# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
