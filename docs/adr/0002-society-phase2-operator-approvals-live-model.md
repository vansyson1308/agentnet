# ADR-0002 — Society Phase 2: operator authorization, durable approvals, live-model hardening, staging topology

- **Status:** Accepted (Phase 2, 2026-09-03)
- **Supersedes / extends:** ADR-0001 (Autonomous Society Runtime v1)
- **Scope boundary:** this ADR ends before any A2A v1 migration, external-agent registration change,
  public third-party agent execution, MCP tool federation or autonomous production deployment.

## Context

ADR-0001 delivered a durable, permissioned closed loop proven with a deterministic `ScriptedRoleModel`.
Phase 2 takes it to a staging system that can be proven with a real model without weakening any v1
invariant. Four gaps had to close first:

1. The v1 observability API exposed context summaries, intent payloads, candidate reports and
   provider configuration to any caller, and "operator" was not a concept the server enforced.
2. `awaiting_approval` intents were persisted but nothing could approve, reject or resume them.
3. `OpenAICompatibleModel` had one request attempt, no usage accounting for retries and no way to
   refuse a credential that had leaked into git history.
4. Staging had no society worker, no migration proof and no scripted smoke/red-team.

## Decisions

### D1 — Operator authorization is one server-side dependency backed by a durable role

`users.society_role` (`operator` | `event_producer`) is the only source of operator authority.
`operator_auth.require_operator` / `require_event_producer` accept **user session JWTs only**: agent
JWTs and scoped `spt_` tokens are refused with 403 even when their owner is an operator, because
scoped tokens are mintable per agent and must never escalate. The `SOCIETY_OPERATOR_BOOTSTRAP_EMAILS`
allowlist exists to bootstrap the first operator on a fresh deployment and is read in that module
only. Roles are assigned by an existing operator (`POST /v1/society/operators`) or the operator CLI;
no intent type can touch them.

Rejected: client-side checks, scattered email comparisons, secret query parameters, an undocumented
universal bearer token, reusing the agent scoped-token system.

### D2 — Public surfaces are structural; everything with content is operator-only

Public: `/status` (flags, fleet role/enabled/paused, counts), `/metrics` (aggregates), `/story/{id}`
(event types, run states, intent type/risk/decision/execution), `/candidates` (id/status/timestamps).
Operator: config, events with payloads, runs with context/decision text, intents with payloads and
policy reasons, candidate specs/reports/paths, budget and wallets, approvals, operators, JARVIS `/ask`.
Tests assert against a private-marker list so a future field cannot leak silently.

### D3 — Human approval is durable, per-intent, re-validated on resume and fails closed

An `approval_required` verdict parks the intent (`awaiting_approval`) and emits
`intent.approval_required`. The decision is recorded in `intent_approvals` (who, decision, reason,
original policy reason, timestamps, final state). Resume is a lease-claimed
(`FOR UPDATE SKIP LOCKED`) execution of the **persisted** intent: the model is never called again,
the executed payload is exactly the row the reviewer saw, and `policy.evaluate_intent(...,
approval_granted=True)` re-runs every other check (grant, flags, scopes, caps). Any change since the
decision denies the intent; forbidden HIGH types are unapprovable at decision and at resume; attempts
are bounded by `SOCIETY_APPROVAL_RESUME_MAX_ATTEMPTS`; a disabled runtime defers instead of
consuming attempts.

Comparison with the OpenAI Agents SDK 0.22.0 human-in-the-loop model (verified 2026-09-03): the SDK
scopes approvals per tool-call id, persists them in `RunState` (`$schemaVersion` 1.17), re-checks the
stored approval status and re-parses arguments against the tool schema on resume, but does **not**
re-evaluate `needs_approval` or compare executed arguments with the reviewed ones. AgentNet keeps the
per-call scope, deliberately offers **no** sticky "always approve", and re-validates the full policy.
The OWASP Agent Control Standard v0.1.0 decision set (`allow | deny | modify | ask | defer`) maps to
`allow`, `deny`, (no `modify` — intents are immutable once persisted), `approval_required` (= `ask`)
and the deferral path while the runtime is disabled; `on_decision_failure: deny` is the default here.

### D4 — World events enter through one guarded ingress

`POST /v1/society/events` requires the `event_producer` or `operator` role, accepts only allow-listed
world event types (`platform.metric.anomaly`, `platform.health.degraded`, `user.feedback.received`,
`staging.canary.signal` + `SOCIETY_INGRESS_EVENT_ALLOWLIST`), refuses reserved society families and
`target_agent_id`, bounds payload size/depth/string length/key count, deduplicates on
`idempotency_key` (a replay answers 200 with the original event) and rate-limits per actor and
globally. Payloads are data, never instructions: the prompt-injection tests inject "call SHELL_EXEC /
GRANT_CAPABILITY" text and assert no forbidden intent is ever allowed. (OWASP Agentic Top 10 2026
ASI01 goal hijack, ASI06 context poisoning, ASI07 inter-agent communication.)

### D5 — Model requests retry under a bounded, observable policy separate from run attempts

`OpenAICompatibleModel` makes at most `1 + SOCIETY_MODEL_REQUEST_RETRIES` attempts, only for
transport errors, timeouts and 408/425/429/5xx, with linear backoff; other statuses raise
`ModelProviderError` immediately with a 200-character excerpt and never a header. Cognition has no
side effects, so replay is safe (the SDK's `replay_safety="safe"` case); run attempts remain the
outer, separately bounded loop. `requests / retries / timeouts` are persisted on `agent_runs` and
summed into the operator budget, and cost accounts for retried input tokens. Structured output is
`response_format=json_object` by default with an opt-in `json_schema` derived from the intent
contract that falls back once on a 400. The SDK's opt-in `ModelSettings(retry=...)` /
`ModelTimeoutError` semantics were the reference.

### D6 — Credential safety is enforced by fingerprint, never by trust

`canary.preflight` reads the credential from the environment only, never prints it, and refuses it
when its SHA-256 fingerprint is in `COMPROMISED_CREDENTIAL_FINGERPRINTS` (three provider-key-shaped
strings found in this repository's git history by the v1 security gate) or matches any key-shaped
string in the checkout's full git history. Reports carry at most an 8-hex fingerprint prefix. A
missing or compromised credential yields `LIVE MODEL BLOCKED — NO SAFE CREDENTIAL`.

**NO FAKE AUTONOMY:** `canary run` / `canary observe` refuse any provider other than
`openai_compatible`, and a report fails if any completed run carries another provider.
`ScriptedRoleModel` / `FakeModel` stay test and demo tools.

### D7 — Staging carries the worker OFF by default; production carries nothing

`docker-compose.staging.yml` adds `society-worker-staging` (registry image, `agentnet_staging` DB,
`SOCIETY_RUNTIME_ENABLED` and `SOCIETY_AUTONOMOUS_CODE_ENABLED` default `false`, no docker socket,
no published ports, healthcheck on the internal metrics port, credential only from the host
environment). `docker-compose.prod.yml` has no society service and no `SOCIETY_*` variable;
`tests/test_society_staging_compose.py` enforces both. Production deploy remains a hard `False` in
code, not a setting. Migrations are proven on fresh and upgrade paths by
`deploy/society-migration-check.sh` (also downgrade/upgrade round-trip).

## References (verified 2026-09-03 from canonical repositories / PyPI; rendered doc sites were unreachable)

| Standard / tool | Version / date | Used for |
| --- | --- | --- |
| OpenAI Agents SDK (Python) | `openai-agents` 0.22.0, 2026-08-19 | HITL `needs_approval` / `RunState` semantics, opt-in retries, `ModelTimeoutError`, `agents.testing.ScriptedModel` |
| OWASP Top 10 for Agentic Applications | 2026 edition, published 2025-12-09 (ASI01–ASI10) | threat mapping for ingress, approvals, identity |
| OWASP Agent Control Standard | spec v0.1.0 (tag v0.1.1, 2026-08-11) | decision vocabulary (`allow/deny/modify/ask/defer`), fail-closed defaults, hook points for future ACS mapping |
| OWASP AI Agent Security / MCP Security cheat sheets | 2026-06-27 / 2026-03-26 | risk-tiered HITL, full-parameter review, least privilege |
| Model Context Protocol | spec 2026-07-28; `mcp` 2.1.1 | stateless per-request auth, no token passthrough, tool allow-listing (future `CALL_EXTERNAL_TOOL`) |
| A2A protocol | v1.0.1 (2026-05-28), stable line 1.0; `A2A-Version: 1.0` | readiness only — **not migrated in Phase 2** |
| a2a-sdk / TCK / Inspector | 1.1.2 (2026-07-22) / 1.0.0.alpha2 / v0.1.0 | readiness planning only |

## Consequences

- Operators must exist before staging is useful: bootstrap one via `SOCIETY_OPERATOR_BOOTSTRAP_EMAILS`,
  then assign durable roles and clear the allowlist.
- Approval is API-only (no UI); `/v1/society/approvals` plus `approve|reject` endpoints are the contract.
- Re-seeding never removes operator gates (`approval_required_intents` is unioned) and the canary never
  seeds an existing fleet.
- Live proof requires a credential the environment can vouch for; without one the honest state is
  `LIVE MODEL BLOCKED — NO SAFE CREDENTIAL`, recorded rather than worked around.
- ACS hook-point emission, `modify` decisions and the A2A 1.0 surface are explicitly deferred.
