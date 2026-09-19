# Society Runtime — staging and live-model runbook (Phase 2)

This runbook takes the Autonomous Society Runtime from the deterministic proof
(`examples/demo_autonomous_society.py`) to a **staging** system driven by a real model.
Production stays OFF throughout: no production worker, no production credential, no
production autonomous code, no production deploy intent (hard `False` in code).

Companion documents: `docs/SOCIETY_RUNTIME.md` (architecture), ADR-0002 (decisions),
`docs/SOCIETY_LIVE_PROOF.md` (what was actually proven, and what was blocked).

---

## 0. Hard rules

| Rule | How it is enforced |
| --- | --- |
| Never use a credential that appeared in git history | `python -m app.society.canary preflight` refuses fingerprints in `COMPROMISED_CREDENTIAL_FINGERPRINTS` and anything key-shaped in the checkout's `git log -p` |
| Never print / commit / trace a credential | scripts read `SOCIETY_MODEL_API_KEY` (or `LLM_API_KEY`) from the environment only; reports carry an 8-hex fingerprint prefix; `/v1/society/config` redacts it; provider errors keep a 200-char body excerpt and no headers |
| NO FAKE AUTONOMY | `canary run/observe` refuse `scripted`/`fake`; a report FAILs if any completed run carries another provider; nobody writes Builder output or QA/Security verdicts by hand |
| Operator authority is server-side | `users.society_role` + one dependency (`operator_auth`); user JWTs only; scoped `spt_` and agent tokens are 403 |
| Runtime OFF by default | `SOCIETY_RUNTIME_ENABLED` / `SOCIETY_AUTONOMOUS_CODE_ENABLED` default `false` in every compose file; the worker idles and touches nothing |
| The credential lives only where the model is called | `SOCIETY_MODEL_API_KEY` is required (fail-fast at startup, `worker.startup_problems`) only by the society worker; on a split deployment (Railway) the registry API mirrors the NON-secret live flags (`SOCIETY_RUNTIME_ENABLED`, `SOCIETY_MODEL_PROVIDER`, `SOCIETY_MODEL_BASE_URL`, `SOCIETY_MODEL_NAME`, profile / thinking / effort / output format, limits, budget) so `/v1/society/status` and `/config` describe the running worker — it never holds the key |
| Production untouched | no production Compose definition is current (the old overlay is retired under `deploy/legacy-vps/`, LEGACY); production autonomous deploy is not a setting | `tests/test_compose_topology.py`, `config.py` |

---

## 1. Staging deployment (standalone Compose project — see `docs/DEPLOYMENT_ARCHITECTURE.md`)

```bash
# on the chosen staging host / container platform; secrets from its environment, never from git
git fetch && git checkout main && git pull
export POSTGRES_HOST=... POSTGRES_USER=... POSTGRES_PASSWORD=... POSTGRES_DB=agentnet_staging \
       REDIS_HOST=... REDIS_PASSWORD=... JWT_SECRET_KEY=... FLASK_SECRET_KEY=... \
       CORS_ALLOWED_ORIGINS=https://<staging-host> PUBLIC_BASE_URL=https://<staging-host> \
       SOCIETY_OPERATOR_BOOTSTRAP_EMAILS=<first-operator@example>
docker compose -f docker-compose.staging.yml config > /dev/null      # renders only with the full env
docker compose -f docker-compose.staging.yml up -d --build           # project agentnet-staging only
bash deploy/society-migration-check.sh --mode local                  # or --mode docker with your containers
SOCIETY_SMOKE_TOKEN=<operator JWT> python3 deploy/society-staging-smoke.py --api http://localhost:8100 --inject
```

The society worker (`agentnet-staging-society-worker`) idles until `SOCIETY_RUNTIME_ENABLED=true` is in
its environment; it gets the repository bind-mounted at `/workspace/repo` (read for worktrees) and a
named volume for workspaces. `docker compose -f docker-compose.staging.yml down` stops only the
`agentnet-staging-*` containers (proven by `tests/test_compose_topology.py`).

## 2. Live-model preflight

```bash
docker exec agentnet-staging-society-worker python -m app.society.canary preflight
```

Verdicts:

| Verdict | `probe.category` | Meaning | Next step |
| --- | --- | --- | --- |
| `LIVE MODEL READY` | `ready` | provider is `openai_compatible`, credential safe, one bounded JSON probe succeeded through the runtime's own request layer | proceed to §3 |
| `LIVE MODEL BLOCKED — NO SAFE CREDENTIAL` | — | no credential, or fingerprint compromised (git history / denylist) | obtain a rotated credential; do **not** test the leaked one |
| `LIVE MODEL BLOCKED — PROVIDER UNREACHABLE` | `misconfigured`, `provider_unreachable` | base URL missing, transport failure or timeout after bounded retries — the provider never answered | fix `SOCIETY_MODEL_BASE_URL` / egress; check `probe.error` |
| `LIVE MODEL BLOCKED — PROVIDER ERROR` | `authentication_failed` (401/403), `rate_limited` (429 after retries), `provider_error` (402, other 4xx, 5xx) | the provider answered with an error status (`probe.http_status`; never the key) | 401: key disabled/rotated; 402: account balance; 429/5xx: provider side — external blockers |
| `LIVE MODEL BLOCKED — OUTPUT CONTRACT` | `empty_content`, `output_truncated`, `output_contract_failed` | the provider answered 200 but `content` was empty, cut off at `max_tokens` (`finish_reason=length`), not JSON, or not the requested object | read `probe.hint`: on DeepSeek set `SOCIETY_MODEL_CAPABILITY_PROFILE=deepseek`, `SOCIETY_MODEL_THINKING_MODE=disabled`, `SOCIETY_MODEL_REASONING_EFFORT=none` (ADR-0007) |
| `LIVE MODEL BLOCKED — PROVIDER IS NOT LIVE (NO FAKE AUTONOMY)` | — | provider is `scripted`/`fake` | this is the correct state for a deployment that has not been given a live model |

The probe (ADR-0007 D4) sends the runtime's exact reasoning policy, `response_format=json_object`, a
json-only prompt with the example `{"ok": true}` and `max_tokens=256`; the report carries structural
metadata only (`finish_reason`, `content_present`, `reasoning_present`, `reasoning_tokens`, requests,
retries, empty-content retries, latency, tokens, capability profile / thinking mode / effort and the request
field names) — never content, reasoning, prompts or the credential. DeepSeek thinks by default (effort
`high`), so the documented live posture is `deepseek` / `disabled` / `none` / `json_object`.

The report also lists the limits the canary will run under (daily USD budget, request retries,
timeout, runs/hour, runs/correlation, causation depth). Tighten them for the first window:
`SOCIETY_DAILY_MODEL_BUDGET=1.0 SOCIETY_MAX_RUNS_PER_HOUR=30 SOCIETY_MAX_RUNS_PER_CORRELATION=12`.

---

## 3. Canaries (real model, staging worker owns the credential)

Enable the runtime for the window (`SOCIETY_RUNTIME_ENABLED=true`, `SOCIETY_MODEL_PROVIDER=openai_compatible`,
restart `society-worker-staging`), then drive each canary **over HTTP** from the host — the canary
process never holds the model credential:

```bash
export SOCIETY_CANARY_TOKEN=...   # operator user JWT
python3 -c 'import sys; sys.path.insert(0,"services/registry")' 2>/dev/null
docker exec -e SOCIETY_CANARY_TOKEN agentnet-staging-registry \
    python -m app.society.canary observe --api http://localhost:8000 --scenario single  --report /tmp/canary-1.json
docker exec -e SOCIETY_CANARY_TOKEN agentnet-staging-registry \
    python -m app.society.canary observe --api http://localhost:8000 --scenario multi   --report /tmp/canary-2.json
# approval interruption: gate one LOW intent for the Scout, then decide from the canary
docker exec agentnet-staging-registry python -m app.society.canary gate --role scout --intent CREATE_IMPROVEMENT
docker exec -e SOCIETY_CANARY_TOKEN agentnet-staging-registry \
    python -m app.society.canary observe --api http://localhost:8000 --scenario approval --decide approve --report /tmp/canary-3a.json
docker exec -e SOCIETY_CANARY_TOKEN agentnet-staging-registry \
    python -m app.society.canary observe --api http://localhost:8000 --scenario approval --decide reject  --report /tmp/canary-3b.json
docker exec agentnet-staging-registry python -m app.society.canary gate --role scout --clear
```

| Canary | Injected event | PASS criteria (evaluated from durable state, never self-reported) |
| --- | --- | --- |
| 1 single agent | `staging.canary.signal` (Scout) | ≥1 completed run, `model_provider=openai_compatible`, model name as configured, no DEAD run |
| 2 multi-agent | `platform.metric.anomaly` | ≥2 roles completed runs, ≥1 causation-linked follow-up event |

The canary's `platform.metric.anomaly` body mirrors the trusted producer's (`telemetry.produce_anomalies`): metric, value, threshold, baseline, sample size, window and `observed_at`, marked `source: staging-canary`. A Scout may only propose on evidence taken from the event, so a body missing those fields is a signal no agent can legitimately act on (Gate A run 12: the live Scout recorded the observation and declined to propose, and the chain never started).

A canary is a REHEARSAL, so it must not leave permanent state behind. Memory is the only runtime state a finished run hands to the next one, and before this rule every rehearsal wrote a durable "this signal was non-actionable" row that the Scout read back as prior experience — on staging all five of its live memory rows were canary residue. Memory written under a correlation a canary started (marked by the injected event's `canary-` idempotency key, which trusted code sets and the model never sees) now expires with the rehearsal (`events.REHEARSAL_MEMORY_TTL_SECONDS`). Rows from real signals keep no expiry, nothing is deleted, and migration `0011_expire_rehearsal_memory` expires the residue written before the rule existed.

Stories injected back-to-back are paced by the driver (§3.1) past the fleet's longest wake cooldown; if one lands inside a cooldown anyway the runtime defers the run (`not_before`, event stays `dispatched`) instead of dropping it. Every model-supplied identifier (`source_task_id`, `proposal_id`, `goal_id`, `task_id`) is validated by the executor before it reaches a foreign key: a fabricated id fails that one intent with a clear reason and the run continues. An EMPTY string for an optional non-text field (`source_task_id`, `thread_id`, `parent_goal_id`, `evidence.sample_size`, …) is read as absent by the strict payload base (Gate A run 9: the live model sent `""` where the schema doc said `"string"`; the doc now says `uuid|null`); a non-empty invalid id, a missing required id and an unknown key are still rejected.
| 3 approval | `platform.metric.anomaly` + gate | intent parked `awaiting_approval`; after `approve` → `executed` (resumed, model not re-called); after `reject` → `rejected`, never executed; `intent_approvals` row present |

Reports contain run ids, roles, statuses, provider/model, tokens, cost, request/retry/timeout
counts, intent types with policy/execution decisions, event types with depth, approvals and candidate
statuses — **no payloads, prompts, context or decision text**.

`canary run` (in-process worker, DB access, credential in this process) exists for local proofs and
is refused for anything but `openai_compatible` too.

### 3.1 On Railway: the Phase 5 driver (`deploy/railway/phase5_live.py`)

From an engineering session that cannot reach the staging edge, the same canaries run from inside the
environment: the `staging-validator` service (docs/RAILWAY_STAGING.md §21) starts
`deploy/railway/${VALIDATOR_SCRIPT:-validate_staging.py}`, so `VALIDATOR_SCRIPT=phase5_live.py` plus a
`PHASE5_PLAN` and a new `VALIDATOR_RUN` value runs one plan per deployment. The driver logs in as the
allow-listed operator (password derived from `STAGING_VALIDATOR_SECRET`, token kept in memory), never
holds the model credential (it is not in its environment) and prints only structural, scrubbed lines
(`PHASE5 <step> <code> PASS|FAIL …`, `PHASE5-JSON <step>.<key> {…}`, `PHASE5 RESULT: OK|FAILED`).

| Step | What it does | Evidence line(s) |
| --- | --- | --- |
| `status` | public flags + a safe subset of `/v1/society/config` (never the credential field) | `status.public`, `status.config` |
| `baseline` | counts-only snapshot of every society table, wallets, tasks, cost today, grants | `baseline.snapshot` |
| `fund:<credits>[:<seq>]` | ledger-consistent DEPOSIT to `Society_Architect`'s wallet: a `pending` transaction completed so the **wallet trigger** credits it (never a balance write; idempotent per `seq`; operator action recorded in `extra_data`) | `fund.result` |
| `gate:<role>:<INTENT>` / `gate:<role>:clear` | operator approval gate on the grant (`approval_required_intents`), only narrowing existing permissions | `gate G01` |
| `canary:<single\|multi\|approval>[:<approve\|reject>]` | `app.society.canary.observe_canary` over HTTP — same PASS criteria as the table above | `canary.<scenario>.report/runs/intents/events/approvals/decisions` |
| `signal[:candidate]` | ONE world event from `PHASE5_SIGNAL_TYPE` + `PHASE5_SIGNAL_JSON`, followed until the story is idle; `candidate` also asserts the engineering chain (READY with QA + Security pass, acceptance tests actually executed, bounded allow-list, one candidate) | `signal.events/runs/intents/decisions/candidates/candidate.<id>`, checks `K01–K10` |
| `taskfail:<credits>[:<seq>]` | proves multi-agent operation on a REAL domain fact: registers two canary agents, funds the caller, creates a task through `POST /v1/tasks`, fails it through `PUT /v1/tasks/<id>/fail`, then waits for the runtime's own `world.ingest_task_outcomes()` to raise `task.failed`. The driver never injects that event and never writes a `task_sessions` row | `taskfail.task/outcome/runs`, checks `T00–T08` |
| `intents:<correlation-id>` | read-only diagnosis of one story: per-intent policy decision, execution status and the **untruncated** validation reason, plus each run's decision summary | `intents.<id>.intents/events/decisions`, check `D01` |
| `memory:<role>` | read-only: how many live memory rows a role carries into its next run, from how many correlations, how many have an expiry, and the newest titles with provenance. Titles are model-authored, so they are scrubbed and bounded; contents are never read and nothing is written | `memory.<role>.view`, check `M01` |
| `audit[:<hours>]` | loop breaker / DEAD / forbidden-HIGH / duplicate-workstream checks, escrow-ledger invariants (`E01–E03`), secret-and-chain-of-thought scan over runtime-produced rows (counts only, `X01–X03`), budget (`C01`), public-surface structure and closed operator surfaces (`P01–P04`) | `audit.snapshot/tasks/transactions/wallets/secret_scan/budget` |

Money invariant (fund step): balances are mutated only by `update_wallet_balances_trigger`; the driver inserts
a `deposit` transaction and flips it `pending → completed`, then proves `balance_after == balance_before + credits`.

Before wake-up: world events injected while the runtime was OFF (every validation's red-team burst and probes)
are still `pending` and would all wake the Scout at activation. They are not edited by hand — the runtime's own
TTL expires them: set `SOCIETY_EVENT_TTL_SECONDS` on the worker to the length of the live window (e.g. `1800`)
before `SOCIETY_RUNTIME_ENABLED=true`; the first dispatch pass marks the backlog `expired: TTL elapsed before
dispatch` (visible in `baseline.snapshot.events_by_status` / `audit`), and fresh canary events dispatch normally.

---

## 4. Soak and GO / NO-GO

Soak window: leave the runtime on with the tightened limits and inject world events only through the
ingress (no manual intermediate events, no hand-written QA/Security marks, no grant inflation, loop
breakers untouched). Read `GET /v1/society/budget`, `/metrics`, `/approvals` and the worker log.

GO requires all of: ≥3 correlations, ≥15 real-model runs, ≥3 roles, ≥1 approval, ≥1 rejection,
≥1 docs-only candidate that reached QA **and** Security with both reports persisted (never merged),
zero DEAD runs caused by the runtime itself, no credential in any log/trace/context, no public-surface
leak (`society-staging-redteam.py` ALL DEFENDED), production flags untouched.

NO-GO (stop the runtime, keep the evidence): any forbidden HIGH intent allowed, any wallet/escrow
inconsistency, any loop breaker tripping repeatedly, a credential appearing anywhere, or the daily
budget exhausted by fewer than the expected runs.

Failure policy: `SOCIETY_RUNTIME_ENABLED=false` (events wait, nothing is lost) → collect
`/v1/society/story/<corr>/detail` for the affected correlation → fix on a branch → re-run the
canaries. Never edit rows by hand to make a canary pass.

---

## 5. Continuous red-team

```bash
SOCIETY_REDTEAM_TOKEN=... SOCIETY_REDTEAM_USER_TOKEN=... SOCIETY_REDTEAM_AGENT_TOKEN=... \
python3 deploy/society-staging-redteam.py --api http://localhost:8100 --burst 40 --report redteam.json
```

Attacks A01–A12 (reserved event families, allowlist, `target_agent_id`, oversize and malformed
payloads, prompt injection through an allow-listed event, operator surfaces with anonymous/user/agent
tokens, approvals on unknown intents, role escalation, idempotent replay, per-actor rate limit,
event_type pattern). Exit code 0 means all defended; `--burst` consumes the actor's hourly quota.

---

## 6. Script environment variables (inputs to scripts, never service settings)

| Variable | Used by | Value |
| --- | --- | --- |
| `SOCIETY_SMOKE_TOKEN` | `deploy/society-staging-smoke.py` | operator user JWT (optional; enables operator checks and `--inject`) |
| `SOCIETY_REDTEAM_TOKEN` | `deploy/society-staging-redteam.py` | operator or event_producer user JWT (required) |
| `SOCIETY_REDTEAM_USER_TOKEN` / `SOCIETY_REDTEAM_AGENT_TOKEN` | red-team | plain user JWT / agent or `spt_` token (optional, adds 403 checks) |
| `SOCIETY_CANARY_TOKEN` | `python -m app.society.canary observe` | operator user JWT (required) |
| `SOCIETY_CANARY_API_URL` | canary `observe` | default `--api` |
| `VALIDATOR_SCRIPT` | Railway `staging-validator` start command | `validate_staging.py` (default) or `phase5_live.py` |
| `VALIDATOR_EXPECT_RUNTIME` | `deploy/railway/validate_staging.py` | `off` (default) or `on`: the public `runtime_enabled` flag the smoke asserts |
| `PHASE5_PLAN`, `PHASE5_TIMEOUT`, `PHASE5_SIGNAL_TYPE`, `PHASE5_SIGNAL_JSON`, `PHASE5_DECIDE`, `PHASE5_OPERATOR_EMAIL`, `PHASE5_FUND_AGENT` | `deploy/railway/phase5_live.py` | plan and inputs of the Phase 5 driver (§3.1); none is a service setting |

Tokens are read, never echoed. Reports are JSON files you choose the path for.

---

## 7. Local dry run of this runbook (no live model)

The whole topology was exercised locally against Postgres with `SOCIETY_MODEL_PROVIDER=scripted`
(registry via uvicorn on :8100, `python -m app.society.worker`, fleet seeded, operator/producer/user/
agent tokens): migration check PASS (fresh + upgrade + round-trip), smoke PASS (C01–C12), red-team
ALL DEFENDED (A01–A12, per-actor limit tripped after 29 events), `canary observe` correctly
**REFUSED** with `PROVIDER IS NOT LIVE (NO FAKE AUTONOMY)`, `canary preflight` reported
`NO SAFE CREDENTIAL` when no credential was present. See `docs/SOCIETY_LIVE_PROOF.md`.
