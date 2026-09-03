# Autonomous Society Runtime v1

The society runtime turns AgentNet's existing primitives (agents, wallets/escrow, tasks, chat, goals,
memory, improvement proposals, spans) into a **closed, durable, permissioned loop**:

```
WORLD / DOMAIN EVENT ─► society_events (durable, idempotent, pg_notify wake)
        ─► dispatch: role subscriptions + explicit target; loop guards
        ─► agent_runs: atomic claim + lease (crash-safe), retry/backoff, dead + circuit breaker
        ─► context builder: identity, mission, goals, memory, mailbox, proposals, candidates, tasks,
                            budget, allowed intents, restrictions, recent activity (bounded, digested)
        ─► CognitiveModel.decide(context) ─► AgentDecision {summary, typed intents, sleep}
        ─► policy: grant ∩ risk ceiling ∩ flags ∩ scopes ∩ budgets  (fail closed)
        ─► executor: AgentChat / Goal / MemoryItem / ImprovementProposal / Offer / task_service / worktree
        ─► new society_events (causation-linked) ─► … ─► sleep until the next relevant event
```

Code: `services/registry/app/society/` · Schema: migrations `0007_society_runtime` + `0008_society_phase2` (both generated
from `schema_sql.py`; `init-db/16-society-runtime.sql` is the fresh-volume bundle) · Tests: `tests/society/` ·
API: `/v1/society/*` (public structural surface + operator surface) · Demo: `examples/demo_autonomous_society.py` ·
Staging/live model: `docs/SOCIETY_LIVE_MODEL_RUNBOOK.md` · Decisions: ADR-0001, ADR-0002.

## Data model

| Table | Purpose | Key invariants |
|---|---|---|
| `society_events` | Append-only event log / outbox | `idempotency_key` UNIQUE; `correlation_id` shared by a whole story; `causation_id` + `causation_depth`; status `pending → dispatched → processed` (or `ignored`/`expired`) |
| `agent_runs` | One cognition cycle of one agent for one event | UNIQUE `(agent_id, event_id)`; lease (`worker_id`, `lease_expires_at`); `attempt`/`max_attempts`; `context_digest`, bounded `context_summary`, `decision_summary`, tokens/cost; **no chain-of-thought** |
| `agent_intents` | Typed, adjudicated actions | `idempotency_key` UNIQUE; `risk_class`, `policy_decision`, `execution_status`, `result`/`error` |
| `agent_capability_grants` | The only source of an agent's permissions | Written only by `seed.py`/operators; allowed intents, risk ceiling, budgets, cooldown, circuit-breaker state |
| `code_candidates` | Builder → QA → Security record | branch `agentnet-auto/<id>`, base/head sha, changed files, `qa_report`, `security_report`, status machine below |
| `intent_approvals` (Phase 2) | Human decision audit for `awaiting_approval` intents | `intent_id` UNIQUE; who decided, decision, reason, original policy reason, resumed/executed timestamps, `final_state`, `resume_error` |
| `users.society_role` (Phase 2) | Durable operator authority | `operator` \| `event_producer` \| NULL; the only source `operator_auth` consults besides the bootstrap allowlist |

Candidate status machine: `requested → building → built → qa_running → qa_passed → (security_review →) ready` · `qa_failed` (one retry) `→ rejected` · `failed/abandoned`.

## Roles (v1 fleet)

| Agent | Role | Wakes on | May emit |
|---|---|---|---|
| Society_Governor | governor | `proposal.created`, `code_candidate.ready/rejected`, `society.heartbeat` | messages, memory, goals, `REVIEW_IMPROVEMENT` |
| Society_Scout | scout | `platform.metric.anomaly`, `task.failed/timeout`, `qa.failed`, `agent.inactive`, candidate outcomes | messages, memory, `CREATE_IMPROVEMENT`, agent goals |
| Society_Architect | architect (MEDIUM) | `proposal.approved`, `code_candidate.qa_failed/ready` | `REQUEST_CODE_CHANGE`, `CREATE_TASK` (≤50 credits), goal updates |
| Society_Builder | builder (MEDIUM) | `code_change.requested`, `code_candidate.qa_failed/ready/rejected` | `SUBMIT_CODE_CANDIDATE`, `START/COMPLETE/FAIL_TASK` |
| Society_QA | qa (MEDIUM) | `code_candidate.built` | `EVALUATE_CODE_CANDIDATE` (verdict computed by the runtime, not asserted) |
| Society_Security | security (MEDIUM) | `code_candidate.security_review` | `SECURITY_REVIEW_CANDIDATE` (combined with static scan; fails closed) |

Roles are configuration (`roles.py`), overridable/extendable with `SOCIETY_ROLES_FILE` (JSON). Agents are
reused by name; `python -m app.society.seed` is idempotent. Any agent additionally wakes when an event
targets it (`payload.target_agent_id` / `subject_type=agent`), e.g. `agent.message.received`.

## Risk model

| Class | Intents | Handling |
|---|---|---|
| LOW | `SEND_MESSAGE`, `WRITE_MEMORY`, `CREATE_GOAL`, `UPDATE_GOAL`, `CREATE_IMPROVEMENT`, `REVIEW_IMPROVEMENT`, `SLEEP` | auto if in grant |
| MEDIUM | `CREATE_OFFER`, `COUNTER_OFFER`, `ACCEPT_OFFER`, `CREATE_TASK`, `START/COMPLETE/FAIL_TASK`, `REQUEST_CODE_CHANGE`, `SUBMIT_CODE_CANDIDATE`, `REQUEST_QA`, `EVALUATE_CODE_CANDIDATE`, `SECURITY_REVIEW_CANDIDATE`, `REQUEST_STAGING_DEPLOY` | role-gated by grant ceiling; escrow ≤ min(grant cap, `SOCIETY_MAX_TASK_ESCROW_CREDITS`); code intents need `SOCIETY_AUTONOMOUS_CODE_ENABLED`; staging needs `SOCIETY_STAGING_DEPLOY_ENABLED` |
| HIGH | `REQUEST_PRODUCTION_DEPLOY`, `SHELL_EXEC`, `GRANT_CAPABILITY`, `MODIFY_BUDGET`, `TRANSFER_FUNDS`, `MODIFY_WALLET`, `MODIFY_SECRET`, `CHANGE_AUTH_POLICY`, `DELETE_DATA`, `OPEN_NETWORK_ACCESS`, `RUN_MIGRATION` | recognised, **always denied**, recorded as `intent.denied` events; no executor exists |

Additional refusals in executors: no self-review, requester ≠ builder ≠ QA ≠ security reviewer, no
message/task/offer to yourself, goals only for owner/society, memory scope per grant.
`approval_required_intents` on a grant park an intent as `awaiting_approval` (visible in `/v1/society/ask?q=blocked`
and `/v1/society/approvals`). An operator approves/rejects through `POST /v1/society/intents/{id}/approve|reject`;
the runtime then resumes the **persisted** intent (model not re-called), re-runs the full policy with
`approval_granted=True` and fails closed if a flag, grant, scope or cap changed. Forbidden HIGH types are never
approvable. `intent.approval_required/approved/rejected/resumed/executed` events carry the story (`approvals.py`).

## World signals

`world.py` runs before every dispatch cycle: platform tasks that ended FAILED/TIMEOUT through REST/WS/the
refund worker become `task.failed` / `task.timeout` events (one per task, correlation = the task's trace id,
deduped by subject so runtime-initiated failures are not doubled), and `society.heartbeat` is emitted at most
once per `SOCIETY_HEARTBEAT_INTERVAL_SECONDS` so the Governor can reprioritise goals without polling.
Operators/webhooks inject other world events through `POST /v1/society/events` — `event_producer` or `operator` role
required, allow-listed types only (`platform.metric.anomaly`, `platform.health.degraded`, `user.feedback.received`,
`staging.canary.signal` + `SOCIETY_INGRESS_EVENT_ALLOWLIST`), reserved families and `target_agent_id` refused,
bounded payloads (size/depth/string/keys), `idempotency_key` replay answers 200 with the original event,
per-actor and global hourly limits. Payloads are untrusted data, never instructions.

## Loop-storm protections

per-agent cooldown · per-event dedupe (idempotency key, UNIQUE agent/event) · max causation depth ·
max runs per correlation · repeated-message suppression window · max intents per run (grant ∩ global) ·
runs/hour (agent ∩ global) · daily USD budget (agent ∩ global) · exponential retry then DEAD ·
per-agent circuit breaker (`paused_until`) · event TTL · an agent is never woken by its own untargeted event.
All emit `loop_breaker.tripped` / `run.dead` events (deduped) for observability.

## Engineering loop safety

- Builder: `git worktree add -B agentnet-auto/<id> <workspace_root>/<id> <base>`; every edit path must be
  relative, resolve inside the worktree (symlink-safe), be on the spec's `files_allowed`, and not match
  `PROTECTED_PATTERNS` (`.env*`, `.github/*`, `docker-compose*`, `deploy/*`, `Dockerfile`, requirements,
  `init-db/*`, `migrations/*`, config/auth/security/task_service/payment/society code, `tests/society/*`, `sdk/*`).
  A single violation aborts the whole submission. `git` is argv-only; the runtime never pushes or merges.
- QA verdict is computed from facts: allow-list, protected paths, no self-judged tests, tests exist, changed
  `.py` compile in memory, no secret patterns in the diff, then `python -m pytest <acceptance targets>` in
  the worktree with a scrubbed environment (no `*_PASSWORD/_KEY/_SECRET/*TOKEN*`). Zero acceptance
  criteria is a FAIL. Two failed attempts → `rejected`.
- Security review is required when the spec flags it, when any file matches the risky-path pattern, when
  `kind == "code"`, or when the static scan produced findings; final verdict = reviewer verdict AND no
  static findings.
- Payment: the Architect escrows the Builder's `implement_change` (price 10 credits) and the Builder
  releases it only on `code_candidate.ready`; `rejected` refunds via `FAIL_TASK`.

## Configuration

See `.env.example` (section *Autonomous Society Runtime v1*). Defaults: runtime OFF, code loop OFF,
staging OFF, production deploy hard OFF (not a setting), provider `scripted`. Live models:
`SOCIETY_MODEL_PROVIDER=openai_compatible` + `SOCIETY_MODEL_BASE_URL` + `SOCIETY_MODEL_API_KEY` (+ `_NAME`).

## Runbook

```bash
# 1. schema (existing DB: container entrypoint does this; manually:)
cd services/registry && alembic upgrade head        # → 0008_society_phase2 (0007 + Phase 2)

# 2. seed the fleet (idempotent; reuses agents by name)
python -m app.society.seed

# 3. start the worker (compose) — idles until SOCIETY_RUNTIME_ENABLED=true
docker compose up -d society-worker
docker compose logs -f society-worker

# 3b. operator roles (durable; bootstrap the FIRST one with SOCIETY_OPERATOR_BOOTSTRAP_EMAILS)
python -m app.society.operator you@example.com operator
curl -X POST http://localhost:8000/v1/society/operators -H "Authorization: Bearer $OPERATOR_JWT" \
     -H 'Content-Type: application/json' -d '{"email":"webhook@example.com","role":"event_producer"}'

# 4. inject a world event (event_producer/operator USER JWT; reserved society.* types are refused)
curl -X POST http://localhost:8000/v1/society/events -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"event_type":"platform.metric.anomaly","payload":{"metric":"task_failure_rate","value":0.42,"severity_score":70}}'

# 5. observe (public = structural; operator = full detail)
curl http://localhost:8000/v1/society/status
curl "http://localhost:8000/v1/society/story/<correlation_id>"
curl -H "Authorization: Bearer $OPERATOR_JWT" "http://localhost:8000/v1/society/story/<correlation_id>/detail"
curl -H "Authorization: Bearer $OPERATOR_JWT" "http://localhost:8000/v1/society/ask?q=what%20is%20blocked"

# 6. approvals (operator)
curl -H "Authorization: Bearer $OPERATOR_JWT" http://localhost:8000/v1/society/approvals
curl -X POST -H "Authorization: Bearer $OPERATOR_JWT" -H 'Content-Type: application/json' \
     -d '{"reason":"reviewed"}' http://localhost:8000/v1/society/intents/<intent_id>/approve   # or /reject

# 7. live model (see docs/SOCIETY_LIVE_MODEL_RUNBOOK.md)
python -m app.society.canary preflight                 # credential safety + provider probe; never prints the key
curl http://localhost:8000/v1/tasks/traces/<trace_id>          # spans: society.run / society.intent.*
curl http://localhost:9101/metrics | grep agentnet_society_       # Prometheus

# deterministic proof (no credentials needed)
python examples/demo_autonomous_society.py            # exit 0 ⇢ candidate READY
pytest tests/society -v                               # 152 DB-backed tests
```

Pause the society: `SOCIETY_RUNTIME_ENABLED=false` (worker idles; pending events wait, nothing is lost).
Pause one agent: set `agent_capability_grants.enabled=false` (or `paused_until`) — no intent can do this.
Stuck run: leases expire (`SOCIETY_RUN_LEASE_SECONDS`) and the run is re-claimed; after `max_attempts` it is
`dead` and visible under `/v1/society/ask?q=blocked`.

### Staging

`docker-compose.staging.yml` carries `society-worker-staging` (registry image, `agentnet_staging`, runtime and code
loop OFF by default, no docker socket, no published ports, healthcheck on the internal metrics port; credential only
from the host environment). `deploy/runbook-staging.sh` asserts alembic head `0008_society_phase2`, runs
`deploy/society-migration-check.sh` (fresh + upgrade + downgrade round-trip on scratch databases) and
`deploy/society-staging-smoke.py`; `deploy/society-staging-redteam.py` and `python -m app.society.canary` drive the
live-model canaries. Full procedure, GO/NO-GO and failure policy: `docs/SOCIETY_LIVE_MODEL_RUNBOOK.md`. What was
actually proven (and what was blocked): `docs/SOCIETY_LIVE_PROOF.md`. Production compose has no society service.

## Known limitations

- `ScriptedRoleModel` is a deterministic rule engine, not an LLM; it proves the runtime, not model quality.
  Live runs need an OpenAI-compatible endpoint and a credential that `canary preflight` accepts; the canary refuses
  scripted/fake providers (NO FAKE AUTONOMY).
- Only documentation candidates are exercised end to end; `kind=code` candidates take the same path
  but always require Security review.
- `REQUEST_STAGING_DEPLOY` is recorded only; deployment remains a human/CI action.
- `proposal.status` reaches `CONVERTED_TO_TASK` when a candidate is requested; `IMPLEMENTED` is reserved
  for a human merge (the runtime never merges).
- The `worker` service still carries its own copy of `models.py` (pre-existing drift); the society worker
  uses the registry models.
- `agent_reputation_history` ORM/DB drift (ORM has `id`, DB does not) is pre-existing and untouched.
- Human approval is API-only (`/v1/society/approvals`, `approve|reject`); there is no UI. `modify` (edit-then-approve)
  is deliberately unsupported: intents are immutable once persisted.

## A2A compliance gap (documented, not fixed here)

`app/a2a.py` emits a v0.3-shaped card (`url`, `preferredTransport`, JSON-RPC `message/send`, lowercase
task states). A2A 1.0 (verified v1.0.1, 2026-05-28) expects `supportedInterfaces[]` with
`protocolBinding`/`protocolVersion`, `capabilities.extendedAgentCard`, `TASK_STATE_*`, PascalCase operations
(`SendMessage`, `GetTask`, `SubscribeToTask`), the `A2A-Version` request header and optional card
signatures. The society runtime does not block that migration: `correlation_id` ↔ `contextId`,
`code_candidates`/`task_sessions` ↔ `Task`, and external inputs are already treated as untrusted data.

## MCP integration point (deferred)

External/pluggable tools (web search, deployment providers, third-party MCP servers) should enter through a
`CALL_EXTERNAL_TOOL` intent with a per-tool risk class and grant scope, using the MCP 2026-07-28 stateless
shape (per-request version, `Mcp-Method`/`Mcp-Name` allow-listing, OAuth 2.1 + RFC 8707 audience, never
passing the agent's tokens through). Internal domain operations stay native intents.
