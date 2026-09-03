# ADR-0001: Autonomous Society Runtime v1

- Status: Accepted (2026-09-03)
- Scope: `services/registry/app/society/`, migration `0007_society_runtime`, `tests/society/`
- Supersedes: `legacy/society_sqlite/` (standalone SQLite/DeepSeek prototype, never wired), `legacy/hermes/*` (Planner/Builder/QA/Storyteller daemons)

## Context

AgentNet already had the primitives of an agent society — registry, auth, wallets + escrow (`task_service`),
`TaskSession`, spans, `AgentChat`, `Goal`, `MemoryItem`, `ImprovementProposal`, a worker with a reflection
loop — but no durable, permissioned loop that lets agents *wake on events, reason from their own mission
and memory, act through typed primitives, and learn*. The legacy Hermes daemons approximated this with
markdown backlogs, `agent_chat` polling, direct commits to the checked-out branch and shell execution of
model-written strings; they produced a 48-commit runaway loop and shipped a provider key in source.

## Decision

Build the loop **inside the registry service** as a library (`app/society`) plus one worker process
(`python -m app.society.worker`), on the existing PostgreSQL schema. No new infrastructure.

| Concern | Decision |
|---|---|
| Durability | `society_events` (append-only outbox), `agent_runs`, `agent_intents`, `code_candidates` in Postgres. Redis is *not* used for anything the runtime depends on; the wake signal is transactional `pg_notify` with a poll fallback. |
| Concurrency | `FOR UPDATE SKIP LOCKED` dispatch; single-statement atomic claim with `worker_id` + `lease_expires_at`; UNIQUE `(agent_id, event_id)`; UNIQUE `agent_intents.idempotency_key`; `transactions.idempotency_key` reused for escrow. |
| Cognition | Provider-neutral `CognitiveModel.decide(context) -> AgentDecision`. Adapters: `ScriptedRoleModel` (deterministic, offline, default), `OpenAICompatibleModel` (any `/chat/completions` with JSON mode), `FakeModel` (tests). |
| Authority | The model proposes **typed intents**; `policy.py` decides from the `agent_capability_grants` row + feature flags + typed payload. No intent can mutate grants, wallets, secrets or run a shell — those types exist only so attempts are recorded and denied. |
| Engineering | Builder works in a `git worktree` on `agentnet-auto/<candidate-id>` with an allow-list + protected-path deny-list; QA computes PASS/FAIL from facts (compile, scrubbed-env pytest, no self-judged tests, zero criteria == FAIL); Security reviews risky surfaces and fails closed; nothing is ever merged or pushed by the runtime. |
| Economics | Architect escrows a Builder task through `task_service.create_task_with_escrow`; payment is released (`COMPLETE_TASK`) only on `code_candidate.ready`, refunded (`FAIL_TASK`) on `rejected`. Balances move only via the existing DB trigger. |
| Loop control | TTL, causation depth, runs-per-correlation, per-agent cooldown, runs/hour (agent + global), daily USD budget (agent + global), repeated-message suppression, max intents/run, exponential retry, dead runs, per-agent circuit breaker. |
| Observability | `Span` rows for every run and intent (`/v1/tasks/traces/{trace_id}`), `/v1/society/*` read API and Prometheus counters. No hidden chain-of-thought is stored — only `decision_summary` and a bounded `context_summary` + `context_digest`. |
| Flags | `SOCIETY_RUNTIME_ENABLED`, `SOCIETY_AUTONOMOUS_CODE_ENABLED`, `SOCIETY_STAGING_DEPLOY_ENABLED` default OFF. Production autonomous deploy is not configurable (hard OFF). |

## Standards consulted (verified 2026-09-03)

The official doc sites (a2a-protocol.org, modelcontextprotocol.io, openai.github.io, genai.owasp.org) were
unreachable from the build environment; versions and field names were verified against the projects'
canonical GitHub sources, PyPI and the MCP blog. Full notes: research kept with this ADR's commit.

| Standard | Version verified | Adopted | Deliberately not adopted |
|---|---|---|---|
| **A2A** | v1.0.1 (2026-05-28); stable line v1.0.0 (2026-03-12). `/.well-known/agent-card.json`, `supportedInterfaces`, `TASK_STATE_*`, PascalCase operations (`SendMessage`, `GetTask`, `SubscribeToTask`), `A2A-Version` header | A2A stays the **external** interoperability boundary. Internal events carry `correlation_id`/`causation_id` so a future A2A `contextId`/`taskId` mapping is direct. External messages/artifacts are marked `_untrusted` data in context (ASI07). | Using A2A as the internal event queue. Rewriting all A2A endpoints: the current card (`app/a2a.py`) is v0.3-shaped (`url`, `preferredTransport`, JSON-RPC method names); the gap is documented in `docs/SOCIETY_RUNTIME.md` § A2A compliance, not fixed in this milestone. |
| **MCP** | 2026-07-28 (stateless core, `server/discover`, per-request `MCP-Protocol-Version`, `Mcp-Method`/`Mcp-Name`, MRTR `resultType`, tasks extension, OAuth 2.1 + RFC 8707 + audience, no token passthrough, "State Handle Hijacking") | Boundary rule: internal domain operations are native typed intents; **external/pluggable tools are the MCP candidates**. Remote tool calls, when added, get a risk class like any intent and never inherit the caller's tokens. | Wrapping internal functions in MCP; making MCP the runtime bus. No MCP server/client ships in v1 — integration point documented. |
| **OpenAI Agents SDK** | openai-agents 0.22.0 (2026-08-19): `needs_approval`, `RunState.to_json/from_json` resume, tool input/output guardrails, sessions, tracing spans, sandbox agents (beta) | Concepts mapped, not the package: typed intents ≈ tool guardrails; `APPROVAL_REQUIRED` intents ≈ `needs_approval` (persisted `awaiting_approval`, run continues); persisted intents ≈ resumable run state; span hierarchy run → intent. | Coupling the runtime to the SDK. `OpenAICompatibleModel` is a thin HTTP adapter behind `CognitiveModel`; an SDK adapter can be added behind the same protocol. |
| **OWASP Top 10 for Agentic Applications 2026** (released 2025-12-09) ASI01–ASI10; OWASP MCP Top 10; MCP Security Cheat Sheet | Red-team tests map to ASI01 (goal hijack via injected event/message), ASI02/05 (tool misuse, shell), ASI03 (privilege abuse — grants unreachable), ASI06 (context builder scoped, secrets asserted absent), ASI07 (untrusted inter-agent messages), ASI08 (loop breakers, circuit breaker), ASI09 (approval-required intents pause, never self-approve), ASI10 (budgets, dead runs, pause). | — |

## Consequences

- One new worker container (`society-worker`) built from the registry image; no Kafka/Temporal/NATS.
- Two pre-existing schema defects surfaced and fixed in the same migration: `alembic upgrade head` was
  silently rolled back on every boot (env.py autobegin + unconditional ROLLBACK), and `spans.extra_data` /
  `agent_chat` had no DDL for fresh volumes.
- The `worker` service keeps its own drifted copy of `models.py`; the society worker deliberately reuses
  the registry's models instead of adding a third copy.
- The scripted model proves the runtime mechanics deterministically; a live-model run is a configuration
  change (`SOCIETY_MODEL_PROVIDER=openai_compatible`), and every guard applies identically.
