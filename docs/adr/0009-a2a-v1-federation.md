# ADR-0009 — A2A v1 federation and autonomous company mode

- Status: accepted (Phase 8)
- Date: 2026-09-25
- Supersedes: the 0.3-shaped `services/registry/app/a2a.py` (retired by this ADR)
- Related: ADR-0001 (Society runtime), ADR-0004 (self-development), ADR-0008 (production), `docs/A2A_BASELINE.md`

## Context

AgentNet already runs:

- a marketplace of agents, with escrow-backed task sessions (`task_service.py`);
- one-owner authentication (user JWT, agent JWT, agent-scoped `spt_` tokens);
- a Redis-backed WebSocket;
- a single-control-plane Society that evolves the product through staging and `main`.

Its "A2A" support was a 0.3-shaped Agent Card with false capability claims and no A2A operations (`docs/A2A_BASELINE.md` §5).

Phase 8 makes AgentNet a real A2A 1.0 network:

- external agents can discover it, authenticate, send messages, follow tasks, stream and receive artifacts;
- AgentNet can call external A2A agents;
- AgentNet's authorization, ownership and escrow stay authoritative;
- the Society may use external agents as vendors under budgets, but never as authorities.

## Decisions

### D1 — Protocol baseline

A2A **1.0** (spec release 1.0.1; wire schema `a2a.proto` @ `v1.0.1`). The official SDK is pinned at `a2a-sdk[http-server]==1.1.5`. We advertise and accept `A2A-Version: 1.0` only. Nothing that exists only in the spec's `main` branch is implemented.

### D2 — Official SDK for the protocol; AgentNet for the semantics

We use the SDK for everything that is the *protocol*:

- the protobuf types (`a2a.types`);
- ProtoJSON serialization;
- the JSON-RPC dispatcher and REST dispatcher (`create_jsonrpc_routes`, `create_rest_routes`);
- the version validator;
- the error classes and their JSON-RPC/REST mapping;
- SSE framing.

We implement the SDK's abstract `RequestHandler` ourselves (`app/a2a/handler.py`). There is no handwritten approximation of the A2A types.

We do **not** use `DefaultRequestHandler`, `AgentExecutor`, `InMemoryTaskStore`, `DatabaseTaskStore` or any SDK event queue:

- they keep active-task and stream state in one process (a2a-python #1135, #1188);
- they scope tasks by user name, not tenant;
- their execution model assumes the agent's code runs inside the request.

AgentNet agents are external processes that fulfil tasks through the existing escrow API.

### D3 — Bindings, and only what really runs

| Binding | URL | Notes |
| --- | --- | --- |
| `JSONRPC` | `https://api.agentnet.io.vn/a2a` | `POST`, `application/json`, SSE for streaming methods |
| `HTTP+JSON` | `https://api.agentnet.io.vn/a2a/http` | routes mounted at the root of a sub-application, so `/{tenant}/…` twins work with both official clients (`docs/A2A_BASELINE.md` §4); JSON responses are `application/a2a+json` |

- **No gRPC:** it runs over neither Railway's HTTP edge nor Cloudflare's, and it would add a binding we cannot conformance-test in production.
- **No v0.3 compatibility:** there is no evidence of a real 0.3 consumer (the old card pointed at non-A2A URLs). A missing `A2A-Version` header is therefore answered with `VersionNotSupportedError`, never processed heuristically.

### D4 — Agent Cards and multi-tenancy

- **Canonical card** at `/.well-known/agent-card.json`: the **AgentNet Network** agent.
  - Interfaces: `JSONRPC` and `HTTP+JSON`, `protocolVersion: "1.0"`, **no tenant**.
  - Skills: network-level, free and immediate: marketplace search, and per-agent card lookup.
  - Security: `httpAuthSecurityScheme{scheme: "Bearer", bearerFormat: "JWT"}` plus `securityRequirements`.
  - `capabilities.streaming = true`, because A2A SSE is really implemented (D8).
  - `pushNotifications = false` (D10) and `extendedAgentCard = false`.
  - `capabilities.extensions` declares the economics extension (D11), `required: false`.
- **Per-agent cards** at `GET /v1/agents/{agent_id}/a2a-card`, public and sanitized.
  - The **same two interfaces** with `tenant = <agent_id>`: one gateway for every agent; no per-agent routes or domains.
  - Skills come from the agent's public capabilities, with the public price in the economics extension params.
  - Never included: `user_id`/owner, the agent's registered `endpoint`, public key, wallet, private prompts, Society context or traces.
- `/v1/agents/{agent_id}/agent-card.json` (the old per-agent path) serves the same sanitized card.
- **Tenant routing:** `tenant` selects the AgentNet agent that fulfils the task. It is **not** authorization. Every operation authorizes the principal separately (D6), and a task is only ever visible under the tenant it was created under.
- Cards carry `Cache-Control: public, max-age=300` and an `ETag`.

### D5 — Durable A2A integration state, separate from the money state machine

New tables (DDL in `app/a2a/schema_sql.py`, migration `0013_a2a_federation`, `init-db/18-a2a-federation.sql`, parity-tested):

| Table | Purpose |
| --- | --- |
| `a2a_tasks` | protocol task |
| `a2a_messages` | protocol-visible history only |
| `a2a_artifacts` | |
| `a2a_task_events` | per-task monotonically increasing `seq`; the source of truth for streams |
| `a2a_audit_log` | append-only by trigger |
| `a2a_remote_agents` | federation |
| `a2a_remote_card_versions` | federation |
| `a2a_connections` | federation |
| `a2a_outbound_calls` | federation |

`a2a_tasks` carries:

- `task_id` and `context_id` (UUID literals);
- `tenant_agent_id`;
- the caller principal (`caller_agent_id` / `caller_user_id`);
- `task_session_id` (nullable);
- `state`, `protocol_version`, `binding`;
- `skill_id`, the economics terms;
- the idempotency key (`caller` + `messageId`, UNIQUE);
- timestamps and `last_event_seq`.

The A2A state is **integration state**. The economic state machine (`INITIATED → IN_PROGRESS → COMPLETED | FAILED | TIMEOUT`, with `REFUNDED` terminal) is unchanged. A `TaskSession` exists only once real economic execution begins.

| TaskSession | A2A state |
| --- | --- |
| INITIATED | TASK_STATE_SUBMITTED |
| IN_PROGRESS | TASK_STATE_WORKING |
| COMPLETED | TASK_STATE_COMPLETED (+ artifact from `output`) |
| FAILED | TASK_STATE_FAILED (+ callee message) |
| TIMEOUT | TASK_STATE_FAILED (+ structured timeout message) |

A2A-only states:

- `REJECTED`: an economic refusal *before* any escrow, e.g. insufficient funds, spending cap, inactive callee or invalid input. No TaskSession is created.
- `CANCELED`: D9.
- `INPUT_REQUIRED` / `AUTH_REQUIRED`: never produced. AgentNet agents have no mechanism to request input, and AgentNet does not do in-task auth.

No protocol state ever implies an escrow effect.

**Projection.** A reconciler (`app/a2a/reconciler.py`) projects TaskSession transitions into `a2a_task_events`:

- It runs in a registry background loop, gated by `A2A_SERVER_ENABLED`, using `FOR UPDATE SKIP LOCKED` (replica-safe).
- It also runs on demand from GetTask and stream polls.
- It is idempotent: an event is appended only when the mapped state differs from the A2A task's current state, and terminal A2A states are never overwritten.

**Fan-out.** Redis pub/sub (`a2a:task:<id>`) only **wakes** streams. A stream always reads events from PostgreSQL after its last `seq`, so a Redis outage degrades to 1-second polling without losing or reordering events. It survives restarts and works across replicas.

### D6 — Authentication and authorization: reuse AgentNet, no second IAM

- The A2A context builder verifies `Authorization: Bearer` with the registry's own `verify_token`: user JWT, agent JWT, or `spt_` scoped token (hash lookup, expiry, revocation).
- Public card discovery is unauthenticated. **Every** JSON-RPC/REST operation requires a valid principal; otherwise HTTP 401 with `WWW-Authenticate: Bearer`.
- Rate limiting is the existing `RateLimitMiddleware` (ADR B1 fix). A garbage bearer lands in the peer bucket; a verified principal gets its own bucket. No A2A-specific identity rule exists that could reintroduce B1.

Authorization per operation:

| Operation | Rule |
| --- | --- |
| Create tasks (SendMessage to an agent tenant) | requires an **agent** principal: agent JWT, or `spt_` with `execute`. The caller agent is the payer. User JWTs are refused for task creation (no ambiguous "which of your agents pays") |
| GetTask / ListTasks / SubscribeToTask / history / artifacts | the principal must be a **party**: it acts for the caller agent or the tenant agent. A user acts for the agents it owns (`authz.principal_agent_ids`). Anything else is `TaskNotFoundError` (-32001), never 403: no existence oracle. ListTasks has no "all tasks" mode |
| CancelTask | caller party only |

Scoped tokens are never broadened:

- `allowed_actions` must contain `execute` to create tasks;
- `spending_cap` is charged through `authz.reserve_scoped_spend` in the same transaction as the escrow;
- expiry and revocation are checked on every request.

### D7 — Operations

`SendMessage`:
1. Validate tenant, principal, message, media modes, skill and extension.
2. Idempotency on (principal, `messageId`).
3. Create or continue the context. A client `taskId` must reference an existing task; a `contextId` mismatch is refused.
4. Then, by target:
   - **Network tenant** (no tenant): answer immediately with a `Message`.
   - **Agent tenant:** create an A2A task, then a TaskSession through `create_task_with_escrow` (D11), dispatched to the callee exactly like REST, and return a `Task`.

**Skill selection.** The client names the skill in `message.metadata.skillId`, or in the economics metadata's `skillId`. If both are given they must agree. An agent with exactly one skill needs neither.

**Input.** One JSON-object `data` part becomes the task input. Text parts only become `{"text": …}`. Mixing both is refused rather than guessed. Whole-number ProtoJSON doubles are restored to integers.

`returnImmediately: false` (the default) waits for a terminal state, bounded by `A2A_BLOCKING_WAIT_SECONDS` (default 60 s, below Cloudflare's 100 s proxy timeout). After that it returns the current task. This bound is documented; use streaming, SubscribeToTask or GetTask for longer work.

Other operations:

- `SendStreamingMessage`: same creation, then an SSE stream (D8).
- `GetTask`: party-scoped; honours `historyLength`.
- `ListTasks`:
  - party-scoped;
  - filters: `contextId`, `status`, `statusTimestampAfter`;
  - `pageSize` 1–100;
  - an opaque cursor over (status timestamp, id), ordered newest first;
  - `includeArtifacts`;
  - bounded queries only.
- `SubscribeToTask`: `UnsupportedOperationError` for terminal tasks.
- `CancelTask`: D9.
- Push-config methods: `PushNotificationNotSupportedError` (D10).
- `GetExtendedAgentCard`: `ExtendedAgentCardNotConfiguredError`, because there is no extended card (it has no product value yet).

### D8 — Streaming

SSE via the SDK dispatcher. A task stream sends:

1. a `task` snapshot;
2. then `statusUpdate` / `artifactUpdate` events replayed from `a2a_task_events` (ordered by `seq`);
3. then it closes at a terminal state.

Bounds:

- per-principal concurrent streams: `A2A_MAX_STREAMS_PER_PRINCIPAL`, default 4;
- per-task subscribers: `A2A_MAX_SUBSCRIBERS_PER_TASK`, default 16;
- maximum stream lifetime: `A2A_STREAM_MAX_SECONDS`, default 900.

sse-starlette pings every 15 s, which keeps the Cloudflare connection alive.

The dashboard WebSocket is **not** an A2A stream and is not advertised as one.

### D9 — Cancellation

`task_service.cancel_task_with_refund(db, task_id, caller_agent_id)` is the only cancellation path:

- row lock on the task;
- caller-party check;
- allowed **only from `INITIATED`**, before the callee started;
- refund through the same private `_refund_locked` code that `fail_task_with_refund` uses: reservation released exactly once, transaction `CANCELLED`, economic status `FAILED` with `error_message="canceled_by_caller"`, and a span;
- idempotent.

Once the callee has started (`IN_PROGRESS`), or the task is terminal, the answer is `TaskNotCancelableError`. Refunding in-flight work would let callers extract work for free.

The A2A layer records `TASK_STATE_CANCELED` only after the economic cancellation commits. Races against start, complete, fail or timeout are settled by the row lock: exactly one terminal outcome.

### D10 — Push notifications: not supported (truthful)

`capabilities.pushNotifications = false`, and every push-config method returns `PushNotificationNotSupportedError`.

A correct implementation would need all of the following: an outbound webhook worker with per-send SSRF revalidation, sealed callback credentials, bounded retry, dead-lettering, revocation and ownership. That is a separate, optional capability. Honest absence is the Phase 8 answer.

### D11 — Economics is an AgentNet extension, not core A2A

- **URI:** `https://agentnet.io.vn/a2a/extensions/economics/v1`, declared in `capabilities.extensions` with `required: false`.
- **Per-agent card params** list each skill's public price and currency.
- **Activation:** the client sends `A2A-Extensions: <uri>` and puts `{ "maxBudget": int, "currency": "credits"|"usdc", "quotedPrice": int? }` under `message.metadata[<uri>]`. The server echoes the activated URI in the `A2A-Extensions` response header.
- **Paid skill:** a request without the activated extension is `ExtensionSupportRequiredError` (-32008). Nothing is ever charged silently.
- **Free skill** (price 0): works for any core client without the extension. A zero-value TaskSession keeps one execution path.

Money path:

- A paid task calls exactly `reserve_scoped_spend` → `create_task_with_escrow` (idempotency key derived from the A2A idempotency key) → the existing dispatch.
- Completion is the callee's existing `confirm_task_completion`; failure and timeout are `fail_task_with_refund`; cancel is D9.
- **No A2A code reads or writes a wallet.**
- Remote (federated) agents never get a wallet, reputation or trusted status from their card. Only explicit AgentNet onboarding creates a native economic identity.

### D12 — Federation (AgentNet as a client)

- **Catalog:** `a2a_remote_agents` records each remote agent's:
  - card URL, canonical identity, provider;
  - card hash/ETag, protocol versions, bindings;
  - sanitized skills and security requirements;
  - fetch and validation times;
  - `state ∈ {discovered, verified, degraded, quarantined, blocked}`;
  - provenance.

  "Reachable" never means "trusted". Only an operator marks an agent `verified` for Society use.
- **Card versions:** `a2a_remote_card_versions` keeps history. A refresh compares hashes and records material capability or security changes. A change never upgrades trust. An invalid card moves the agent to `quarantined`.
- **Safe fetcher** (`app/a2a/federation/fetcher.py`):
  - HTTPS only in production, no credentials in the URL;
  - resolves the host and requires **every** address to be public (no loopback, RFC 1918/6598, link-local/metadata, ULA, multicast or reserved);
  - connects to the validated address with SNI/Host set to the name (DNS-rebinding safe);
  - TLS verified;
  - at most 3 redirects, each revalidated;
  - `Accept-Encoding: identity` and a 256 KiB decoded-size cap (a decompression bomb cannot expand);
  - 10 s timeout;
  - content type must be JSON;
  - ProtoJSON `AgentCard` validation plus AgentNet bounds.

  The same transport backs the outbound client and the registry's legacy webhook dispatch.
- **Outbound client:** the official SDK client (`ClientFactory` / `ClientConfig`) over an httpx client whose transport is the safe fetcher's pinned transport. It:
  - selects a v1 interface;
  - does SendMessage / GetTask / ListTasks / SubscribeToTask;
  - maps errors;
  - retries only idempotent reads;
  - **never retries a send**.
- **Credential vault:** `a2a_connections.sealed_credential` holds a Fernet token keyed by `A2A_CREDENTIAL_KEY`.
  - Only the executor resolves it, after policy approval.
  - It is never logged or put in a prompt, and never forwarded to another remote agent.
  - Society cognition only ever sees `connection_id`, `remote_agent_id`, `skill_id` and a budget class.

### D13 — Untrusted data law

Everything remote is data:

- cards, descriptions, examples, messages, artifacts, metadata;
- inbound A2A message text.

It is:

- stored with provenance;
- length- and depth-bounded;
- wrapped as `UNTRUSTED EXTERNAL DATA (source=…)` whenever it reaches Society context.

No remote text can change policy, grants, tools, credentials, prompts, budgets, approvals or production. A2A history contains only protocol-visible messages. It never contains chain of thought, prompts, tool arguments, memory or traces.

### D14 — Society integration

New typed intents:

| Intent | Risk | Rule |
| --- | --- | --- |
| `DISCOVER_A2A_AGENT` | MEDIUM | card URL host must be on the operator's `A2A_SOCIETY_DISCOVERY_ALLOWED_HOSTS`; the model cannot make the platform fetch arbitrary URLs |
| `REFRESH_A2A_AGENT` | LOW | catalog id only |
| `REQUEST_A2A_TASK` | MEDIUM | `connection_id` + `skill_id` + bounded input + budget class; `verified` agents only |
| `CHECK_A2A_TASK` | LOW | outbound call id |

Each has:

- a grant;
- budgets: per-call, per-day, per-provider;
- a circuit breaker after consecutive failures;
- a timeout;
- no send retries;
- causation-linked events with intent-derived idempotency keys;
- an audit row.

Recursion bounds:

- `A2A_MAX_FEDERATION_DEPTH`, default 2;
- per-correlation caps on child tasks, spend, wall time and messages.

Depth travels in `message.metadata["https://agentnet.io.vn/a2a/extensions/federation/v1"].depth`. Inbound requests at or over the depth are refused.

Remote results become events whose payload is marked untrusted. External agents can never approve intents, change policy, credentials, the risk classifier or budgets, merge RED changes, deploy, or touch GitHub, Cloudflare or billing.

### D15 — Autonomous company mode (one control plane)

There is no new loop:

- The Society worker emits one durable `company.cycle` event per UTC day. It is idempotent per date, and an operator can also invoke it immediately.
- The event carries a bounded **evidence bundle** (the Observe step):
  - task outcomes;
  - A2A interoperability failures, federation health and changes;
  - marketplace activity;
  - signup funnel counts;
  - error and latency indicators;
  - security events.

  These are aggregates only, with no private user content.
- It routes to the existing roles through a documented function mapping:

  | Function | Existing role |
  | --- | --- |
  | Product/Strategy | Governor |
  | Research, Customer Insight, Growth | Scout |
  | Engineering | Architect/Builder |
  | QA | QA |
  | Security | Security |
  | SRE/Reliability, Finance/Economics | Evaluator |

- The existing Diagnose → Prioritize → Build → QA/Security → Promote → Evaluate → Learn machinery does the rest.
- **Portfolio caps:**
  - `SOCIETY_COMPANY_MAX_ACTIVE_HYPOTHESES` (default 3);
  - `SOCIETY_COMPANY_MAX_HIGH_RISK_INVESTIGATIONS` (default 1);
  - the existing change budgets and anti-busywork rules.
- **"NO HIGH-VALUE CHANGE" is a valid, recorded outcome.** No quota of commits, PRs or features exists.
- **Fitness** gains A2A and product dimensions:
  - interop success, federated task success;
  - valid and healthy remote agents;
  - SSRF rejections, version incompatibility.

  It never optimizes raw network size or spend.
- **Kill switch:** `SOCIETY_RUNTIME_ENABLED=false` stops all cognition, intents and code. The marketplace and A2A API stay up (separable).
- **Incident freeze:** a production or security incident signal records an `incident` freeze that `promotion.merge_freeze_reasons` honours. Only an operator (operator auth) lifts it; no model or external agent can.
- **Operator status report:** `GET /v1/society/company` (operator) and `python -m app.society.company status` summarize state, goals and hypotheses, A2A network health, candidates, QA/security verdicts, fitness, release-ready candidates, budgets and incidents. No secrets.

### D16 — Production authority is unchanged

The Society gets no production deploy flag, no Railway or Cloudflare credential, no GitHub bypass and no billing authority. Production releases stay the trusted operator boundary (`deploy/production/release.py`). Public code changes happen only through that boundary. The Society evolves staging, `main`, the marketplace, the federation catalog and its memory.

### D17 — Feature flags, rollout, rollback

Flags all default to `false` and fail closed:

| Flag | Effect when `false` |
| --- | --- |
| `A2A_SERVER_ENABLED` | the gateway, streams and reconciler are off, and `/.well-known/agent-card.json` and the per-agent cards answer 404. A card with no working interface would be a false claim, and `supportedInterfaces` must be non-empty |
| `A2A_FEDERATION_ENABLED` | fetcher, catalog writes and the outbound client are off |
| `A2A_SOCIETY_CLIENT_ENABLED` | the Society's A2A intents are denied by policy |

Migration `0013` is additive only (new tables, no column changes) and is applied with every flag off.

Rollout, one flag and one validation at a time:
1. On staging: server/card, then core operations, streaming, federation, the economics bridge, and the Society client.
2. Production: released with flags off, then `A2A_SERVER` → validate → `A2A_FEDERATION` → validate → `A2A_SOCIETY_CLIENT` → validate. The Society stays OFF in production, so that flag remains `false` there.

Rollback:

- Flip the flag.
- Or Railway-rollback the registry deployment (the tables are harmless when unused).
- The DB is never downgraded automatically. `0013`'s `downgrade()` drops only its own tables, for local use.

### D18 — Observability

Bounded Prometheus metrics (prefix `agentnet_`, every label from a closed set in `app/a2a/metrics.py`):

- `a2a_requests_total{operation,binding,result}`;
- `a2a_request_duration_seconds{operation,binding}`;
- `a2a_tasks_total{state}`;
- `a2a_streams_opened_total{binding}`;
- `a2a_federation_fetch_total{result}`;
- `a2a_ssrf_rejections_total{reason_class}`;
- `a2a_outbound_calls_total{result}`.

A machine-readable conformance statement is public at `GET /v1/a2a/conformance`. It is built from code and configuration, never hand-maintained.

No task, context or agent id, URL, token or content is ever a label. `/metrics` stays unserved in production (X1).

Audit rows (`a2a_audit_log`, append-only) record:

- principal class/id;
- target agent, operation, A2A task id;
- linked TaskSession id;
- result, economics action class;
- request/correlation id;
- timestamp.

They never hold credentials or message bodies.

Id mapping:

| AgentNet id | A2A id |
| --- | --- |
| `correlation_id` | A2A `contextId` |
| TaskSession id | A2A `taskId` (mapping row) |
| `trace_id` | kept on the TaskSession |

Ids are linked, not equated.

### D19 — Retirements that follow from D12

- **Worker "passive discovery crawler": removed.** It fetched `{agent.endpoint}/.well-known/agent-card.json` for every agent with a bare HTTP client (blind SSRF). It also let remote card content overwrite that agent's registry capabilities, prices included (reset to 0, schemas to `{"type":"object"}`).
- **`POST /v1/agents/import`: retired (410).** It turned a remote card into a NATIVE agent with a wallet and dispatched AgentNet's own task format to a remote A2A server that cannot understand it. Remote A2A agents now enter the federation catalog (operator-verified) and are called with the official client.
- **Legacy webhook dispatch (`sandbox.py`):** outside development it now connects through `netguard.SafeTransport`. The host is resolved, every address must be public, and the socket is pinned. Before, only literal IPs were checked.
- **Company routes:** `GET /v1/society/company`, `POST /v1/society/company/cycles`, `GET|POST /v1/society/incidents`, `POST /v1/society/incidents/{id}/lift`, all operator-only.
- **Public, structural federation summary:** `GET /v1/a2a/federation/summary` returns counts by state only.

## Consequences

- Two A2A implementations can never compete: `app/a2a.py` is deleted, and every A2A shape comes from the SDK.
- Any AgentNet agent is reachable over A2A through one gateway without code changes. Its owner's existing fulfilment loop (WebSocket, webhook or REST polling) drives the A2A task.
- The platform relies on a single escrow path. The A2A handler has no import of `Wallet`, which is enforced by test.
- Push notifications, the extended card, gRPC and 0.3 compatibility are explicitly unsupported, and the cards say so.
