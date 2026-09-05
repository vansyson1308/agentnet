# Test matrix (Phase 2.5 §18 — no silent exclusions)

Every test file in the repository is classified below. CI runs everything
marked **RUNS IN CI** in one job (`pytest tests/ --ignore=tests/test_integration.py`)
plus the dedicated fresh-install job; `scripts/ci/check_skips.py` fails the
build on any skip whose reason is not on the explicit allow list. `pytest.ini`
turns every repo-owned deprecation class into an error and makes
`@pytest.mark.timeout` real (Phase 2.6, ADR-0003 D8). Nothing is
excluded by habit: the former `--ignore=tests/test_simulation*.py` exclusions
are gone (those suites pass).

| File | Class | Needs | Notes |
| --- | --- | --- | --- |
| `tests/test_alembic.py` | RUNS IN CI | — | migration graph shape |
| `tests/test_app_lifespan.py` | RUNS IN CI | — | lifespan replaces on_event: order, exactly-once startup/shutdown, tracer flushed synchronously (Phase 2.6) |
| `tests/test_pytest_timeout_plugin.py` | RUNS IN CI | — | pytest-timeout loaded; a 1 s marker kills a hanging test; without the plugin the mark is an error |
| `tests/test_pydantic_v2_config.py` | RUNS IN CI | — | every response model: ConfigDict(from_attributes) and an attribute-style round trip |
| `tests/test_sqlalchemy_base.py` | RUNS IN CI | — | DeclarativeBase in all four services, one metadata each |
| `tests/test_runtime_contract.py` | RUNS IN CI | — | image/CI Python pins, bcrypt pin, hashing without stdlib `crypt` |
| `tests/test_approval_workflow.py` | RUNS IN CI | — | payment approval state machine |
| `tests/test_compose_topology.py` | RUNS IN CI | docker CLI | one test (`--dry-run down`) skips with an explicit reason when no daemon; ownership is proven on rendered config |
| `tests/test_cors_security.py` | RUNS IN CI | — | |
| `tests/test_db_parity.py` | RUNS IN CI | PostgreSQL | ORM ⇄ DDL ⇄ migration introspection (77 tests) |
| `tests/test_fresh_install.py` | RUNS IN CI (own job) | PostgreSQL, `redis-server`, free ports | skips unless `AGENTNET_FRESH_INSTALL=1`; the fresh-install job sets it and forbids skips |
| `tests/test_health_metrics.py` | RUNS IN CI | — | readiness never leaks connection details |
| `tests/test_integration.py` | EXTERNAL-SERVICE | live registry + payment + worker (`REGISTRY_URL`, `PAYMENT_URL`, `POSTGRES_*`) | rewritten as an asserting E2E (register → verify → agents → dev funding → escrow create/start/confirm → worker refund → cross-tenant refusal → ledger invariants); executed by the fresh-install harness against the services it boots; module-skips only when no live registry answers |
| `tests/test_logging_config.py` | RUNS IN CI | — | |
| `tests/test_money_invariants.py` | RUNS IN CI | — | trigger/app single-writer invariants |
| `tests/test_no_hardcoded_secrets.py` | RUNS IN CI | — | secret literal scan |
| `tests/test_phase_d.py` | RUNS IN CI | — | legacy files now under `deploy/legacy-vps/`; proxy-header trust is env-driven |
| `tests/test_platform_fee.py` | RUNS IN CI | — | |
| `tests/test_rate_limiting.py` | RUNS IN CI | — | includes X-Forwarded-For spoofing guard |
| `tests/test_sandbox.py` | RUNS IN CI | — | previously never run in CI; passes |
| `tests/test_sdk.py` | RUNS IN CI | — | includes a real local WebSocket server (connect/send/recv/close/reconnect/headers); also run at websockets 12.0 and the current release by `scripts/ci/check_sdk_envs.sh` |
| `tests/test_secrets_required.py` | RUNS IN CI | — | fail-fast config incl. `INTERNAL_WORKER_TOKEN` |
| `tests/test_simulation.py`, `tests/test_simulation_escrow.py`, `tests/test_simulation_seed.py` | RUNS IN CI | — | previously ignored in CI; pass |
| `tests/test_task_contract.py` | RUNS IN CI | — | |
| `tests/test_task_service.py` | RUNS IN CI | — | |
| `tests/test_tracing.py` | RUNS IN CI | — | placeholder "verified by inspection" tests replaced by real OTLP exporter assertions |
| `tests/test_worker_lifecycle.py` | RUNS IN CI | — | SIGTERM/stop, DB outage, Redis outage/reconnect, poll floor |
| `tests/test_ws_escrow.py` | RUNS IN CI | — | WS/REST escrow parity |
| `tests/society/*` (23 files) | RUNS IN CI | PostgreSQL | whole package skips with a reason if Postgres is unreachable; CI provides it. Includes `test_authz_registry.py`, `test_authz_payment.py`, `test_money_concurrency.py` (real-Postgres races), `test_websocket_auth.py`, `test_sandbox_paths.py`, `test_config_validation.py`, `test_canary.py` (deterministic adapters only — never a live model) |
| `tests/society/acceptance/test_candidate_docs.py` | TEST FIXTURE | — | executed by the Society QA stage inside candidate worktrees, not collected as a repo test |
| `examples/demo_autonomous_society.py` | RUNS IN CI (step) | PostgreSQL | deterministic Society end-to-end |
| `deploy/society-migration-check.sh --mode local` | RUNS IN CI (step) | PostgreSQL | fresh→head, pre-society→head, head no-op, downgrade/upgrade |
| `scripts/ci/check_service_envs.sh` | RUNS IN CI (own job, Python 3.10) | — | each service's requirements alone: pip check, import smoke, shared-pin agreement |
| `scripts/ci/check_sdk_envs.sh` | RUNS IN CI (own job) | — | SDK tests at websockets 12.0 and the current release |
| `scripts/ci/check_images.sh` | RUNS IN CI (docker-build step) | docker | import smoke inside every built image |
| `deploy/society-staging-smoke.py`, `deploy/society-staging-redteam.py` | EXTERNAL-SERVICE | a running staging stack | operator scripts, not pytest |
| `python -m app.society.canary` | LIVE-CREDENTIAL | model credential + staging | never run in CI by design (`NO LIVE MODEL KEY`) |

Classes: RUNS IN CI · DOCKER-ONLY (none remain) · EXTERNAL-SERVICE · LIVE-CREDENTIAL · OBSOLETE (none) · BROKEN TEST (none) · TEST FIXTURE.
