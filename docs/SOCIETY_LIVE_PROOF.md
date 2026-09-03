# Society Runtime — live proof record (Phase 2)

Status line: **LIVE SOCIETY PARTIAL — BLOCKED** (as of 2026-09-03).
Everything that could be proven without a live model credential and without reaching the staging
host has been proven and is reproducible; the real-model canaries and the soak are blocked by two
external conditions recorded below. Nothing in this document was produced by a scripted model
presented as live, by manual intermediate events, or by hand-written QA/Security verdicts.

## 1. What is proven (reproducible from this repository)

| Gate | Command | Result |
| --- | --- | --- |
| Full test suite (CI scope) | `pytest tests --ignore=tests/test_integration.py` against PostgreSQL | 420 passed, 0 failed, 0 skipped |
| Society package | `pytest tests/society` | 152 passed (operator auth matrix, public sanitisation, ingress limits + prompt injection, approval lifecycle incl. 6-thread decision race, resume lease crash recovery, fail-closed re-check, live-adapter retries/accounting, canary credential safety, HTTP-driven canary) |
| Migration proof | `deploy/society-migration-check.sh --mode local` | PASS: fresh bundle → head `0008_society_phase2` idempotent; upgrade path ran 0007 and 0008; second upgrade no-op; downgrade to 0007 removed `intent_approvals`/`users.society_role`; re-upgrade restored them; all Phase 2 columns/indexes/uniqueness present on both paths |
| Compose topology | `docker compose config` (dev), `-f docker-compose.yml -f docker-compose.staging.yml config`, `-f docker-compose.prod.yml config` | all valid; `tests/test_society_staging_compose.py` enforces OFF-by-default, no socket, no ports, staging DB, no society service in prod |
| Local staging-like topology | scratch DB `agentnet_local_stage` (init-db bundle + alembic head), registry via uvicorn `:8100`, `python -m app.society.worker` (`SOCIETY_MODEL_PROVIDER=scripted`, runtime ON, code loop OFF), seeded fleet, operator / event_producer / user / agent tokens | worker log: 0 tracebacks; 1 completed Scout run per injected story, subsequent wakes correctly `skipped` by cooldown |
| Staging smoke | `deploy/society-staging-smoke.py --expect-runtime on --inject --metrics-probe 127.0.0.1:9101` | **PASS C01–C12**: health, public status (production deploy OFF, fleet 6), public surfaces free of private fields, anonymous 401 on every operator path, operator config redacts the credential, approvals/budget listings, event accepted once and replay answered 200 as duplicate, public story sanitised, run completed with its provider reported on the operator detail, metrics port unreachable |
| Staging red-team | `deploy/society-staging-redteam.py --burst 40` (event_producer token) and without burst (operator token) | **ALL DEFENDED A01–A12**: reserved families 400, allowlist 400, `target_agent_id` 422, oversize 413, malformed shapes 422, prompt injection accepted as data only (marker never public, no forbidden HIGH intent allowed/executed, production deploy OFF), anonymous/user/agent 401/403, unknown intent 404 / non-operator 403, role escalation 422/403, replay 200 same id, per-actor limit tripped after 29 events, event_type pattern 422 |
| NO FAKE AUTONOMY | `python -m app.society.canary observe --scenario single` against that topology | **REFUSED**: `LIVE MODEL BLOCKED — PROVIDER IS NOT LIVE (NO FAKE AUTONOMY): deployed model_provider='scripted'` |
| Credential safety | `python -m app.society.canary preflight` (no credential); with `SOCIETY_MODEL_PROVIDER=openai_compatible` and a base URL but no key | `PROVIDER IS NOT LIVE` / `LIVE MODEL BLOCKED — NO SAFE CREDENTIAL`; unit tests prove a key committed-then-removed in git history is refused and that reports never contain the key |

One defect was found by the scripts and fixed before merge: an idempotent replay of a world event
answered 201; it now answers 200 with the original event, and a lost race between two identical
POSTs is also reported as a duplicate.

## 2. What is blocked, and why

| Item | State | Evidence |
| --- | --- | --- |
| Live model credential | **none available** — `LIVE MODEL BLOCKED — NO SAFE CREDENTIAL` | no `SOCIETY_MODEL_API_KEY` / `LLM_API_KEY` in the execution environment; no `.env`; the only provider keys in existence for this project appeared in git history and are compromised (their SHA-256 fingerprints are on the denylist; the values were never used or printed) |
| Staging deployment | **unreachable from this environment** | egress to `staging.agentnet.io.vn` is refused by the sandbox proxy (CONNECT → 403 policy denial); `deploy/runbook-staging.sh` must be executed on the staging host by an operator |
| Canary 1–3 with a real model | not run | require both items above; the procedure and PASS criteria are in `docs/SOCIETY_LIVE_MODEL_RUNBOOK.md` §3 and the driver is `python -m app.society.canary observe` |
| Soak (≥3 correlations, ≥15 real-model runs, ≥3 roles, ≥1 approval, ≥1 rejection, ≥1 docs candidate with QA + Security persisted) | not started | criteria unchanged; the docs-only Builder candidate path is proven deterministically (`examples/demo_autonomous_society.py`, `tests/society/test_e2e_autonomous_loop.py`) but must be re-proven with the live model before GO |
| GO / NO-GO | **not reached** | see status line |

## 3. What an operator must do to finish the proof

1. Obtain a **rotated** OpenAI-compatible credential from the secret store; never the leaked one.
2. On the staging host: pull `main`, set `SOCIETY_OPERATOR_BOOTSTRAP_EMAILS`, run `deploy/runbook-staging.sh`
   (migration proof + smoke run automatically), bootstrap the first operator.
3. `docker exec agentnet-staging-society-worker python -m app.society.canary preflight` → must print `LIVE MODEL READY`.
4. Enable the runtime for the window with tightened limits, run canaries 1–3 (`canary observe`), keep the
   JSON reports, run the red-team again with the runtime on.
5. Soak until the criteria in §4 of the runbook are met, then record GO/NO-GO here with the report paths.
6. Production stays OFF regardless of the outcome; A2A migration starts only after GO and only as a
   separate mission (`docs/A2A_V1_READINESS.md` is written only after GO).

## 4. Integrity statement

- No `ScriptedRoleModel`/`FakeModel` run is presented as live; the canary tooling refuses them.
- No manual intermediate event, Builder output, QA verdict or Security verdict was written by hand.
- No grant was inflated; loop breakers and budgets were left at or below defaults.
- No production setting was touched; `docker-compose.prod.yml` carries no society service.
- No credential was printed, committed, traced or placed in a context; the compromised fingerprints
  are hashes only.
