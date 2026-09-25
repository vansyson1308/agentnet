# A2A protocol baseline (pinned for Phase 8)

Researched 2026-09-25. Only official sources were used:

- the A2A specification repository at its release tags;
- the official Python SDK (`a2a-sdk`) and JS SDK (`@a2a-js/sdk`);
- the official samples;
- the registries (PyPI, npm).

`a2a-protocol.org` could not be reached from the build sandbox, so every spec fact comes from the source that builds that site: the spec repository at tag `v1.0.1`. Facts marked **[probe]** were observed by running the pinned SDK.

## 1. What is pinned

| Component | Version | Source |
| --- | --- | --- |
| A2A protocol (advertised and sent) | **`1.0`** — the protocol line is Major.Minor; patch versions are never advertised | https://github.com/a2aproject/A2A/blob/v1.0.1/docs/specification.md (§ versioning) |
| Latest released spec | **1.0.1** (2026-05-28), a patch over 1.0.0 (2026-03-12). No 1.1 release exists | https://github.com/a2aproject/A2A/releases/tag/v1.0.1 |
| Normative wire schema | `specification/a2a.proto` at `v1.0.1`, package `lf.a2a.v1`. The wire schema is unchanged between 1.0.0 and 1.0.1 | https://github.com/a2aproject/A2A/blob/v1.0.1/specification/a2a.proto |
| Python SDK | **`a2a-sdk[http-server]==1.1.5`** (2026-09-21, Python ≥ 3.10, protocol 1.0 + optional 0.3 compat) | https://pypi.org/project/a2a-sdk/1.1.5/ |
| JS SDK (interop proof only) | **`@a2a-js/sdk@1.2.1`** (2026-09-24, Node ≥ 20, protocol 1.0) | https://www.npmjs.com/package/@a2a-js/sdk/v/1.2.1 |
| Reference server (interop proof only) | a2a-samples `samples/python/agents/helloworld`, run on the pinned `a2a-sdk` | https://github.com/a2aproject/a2a-samples/tree/main/samples/python/agents/helloworld |

The registry image installs `a2a-sdk[http-server]==1.1.5`. With the registry's existing pins, this resolves on Python 3.10 to:

- `protobuf 7.36.2`;
- `sse-starlette 3.4.11`;
- `starlette 1.7.0`;
- `google-api-core 2.39.0`;
- `json-rpc 1.15.0`;
- `culsans 0.11.0`.

`pip check` is clean.

## 2. v1.0.1 errata that matter here

- HTTP+JSON SHOULD use `application/a2a+json` for requests and responses; 1.0.0 said `application/json`.
- Error mappings were rewritten:

  | Error | Before | After |
  | --- | --- | --- |
  | TaskNotCancelable | 409 | 400 |
  | ContentTypeNotSupported | 415 | 400 |
  | InvalidAgentResponse | 502 | 500 |

  Most gRPC `UNIMPLEMENTED` statuses became `FAILED_PRECONDITION`.
- JSON-RPC `error.data` and REST `error.details` are arrays of `@type` objects. REST MUST carry a `google.rpc.ErrorInfo` with domain `a2a-protocol.org`.
- The prose now spells TaskState as `TASK_STATE_*`.

## 3. Wire facts AgentNet implements

- **JSON-RPC methods:**
  - `SendMessage`, `SendStreamingMessage`;
  - `GetTask`, `ListTasks`, `CancelTask`, `SubscribeToTask`;
  - `Create/Get/List/DeleteTaskPushNotificationConfig(s)`;
  - `GetExtendedAgentCard`.

  The old 0.3 names (`message/send`, …) are **not** served.
- **HTTP+JSON routes**, relative to the interface URL, with no `/v1` prefix:

  | Route | Operation |
  | --- | --- |
  | `POST /message:send` | Send message |
  | `POST /message:stream` | Send streaming message |
  | `GET /tasks/{id}` | Get task |
  | `GET /tasks` | List tasks |
  | `POST /tasks/{id}:cancel` | Cancel task |
  | `GET\|POST /tasks/{id}:subscribe` | Subscribe to task |
  | `…/pushNotificationConfigs` | Push configs |
  | `GET /extendedAgentCard` | Extended card |

  Each route has a `/{tenant}/…` twin.
- **Version:** header `A2A-Version: 1.0`. A missing header means 0.3, which AgentNet refuses with `VersionNotSupportedError`: JSON-RPC `-32009` / REST HTTP 400 `VERSION_NOT_SUPPORTED`.
- **Enums** are serialized by name (`TASK_STATE_WORKING`, `ROLE_USER`). `kind` discriminators are gone. `Part` is a oneof of `text | raw | url | data`, plus `mediaType`, `filename` and `metadata`.
- **Streaming** is SSE (`text/event-stream`). Every event is a `StreamResponse`: `task | message | statusUpdate | artifactUpdate`.
  - A task stream starts with a `task`.
  - There is no `final` flag; the stream ends by closing.
  - `SubscribeToTask` sends a `task` snapshot first.
- **Tasks:**
  - `SubscribeToTask` on a terminal task returns `UnsupportedOperationError` (-32004).
  - `CancelTask` on a task that cannot be canceled returns `TaskNotCancelableError` (-32002).
  - `ListTasks` `pageSize` runs 1–100 (default 50). `nextPageToken` is always present and is `""` on the last page.
- **`tenant`** is an opaque routing string copied from `AgentInterface.tenant`. It travels in the REST path or in the request message's `tenant` field. It is **not** an authorization boundary on its own.
- **Extensions:**
  - declared in `capabilities.extensions[{uri, description, required, params}]`;
  - requested with the `A2A-Extensions` header;
  - a required-but-absent extension returns `ExtensionSupportRequiredError` (-32008).

## 4. Known upstream issues and deviations (a2a-sdk 1.1.5)

| Issue | Consequence for AgentNet |
| --- | --- |
| `DefaultRequestHandler` (V2) keeps active-task/streaming state in one process and ignores `queue_manager` (a2a-python #1135, closed with a warning; #1188 still open: stale snapshot across replicas) | AgentNet does **not** use `DefaultRequestHandler`, `AgentExecutor`, `InMemoryTaskStore` or any SDK queue. It implements the SDK's `RequestHandler` interface over its own PostgreSQL event log plus Redis fan-out |
| Resubscribe can lose events with in-memory queues (#832, closed "not planned") | Events are durable rows with a per-task sequence. A subscriber replays from the database |
| REST tenant routes break when `create_rest_routes(path_prefix=…)` is used (server mounts `/{tenant}/prefix/…`, client calls `/prefix/{tenant}/…`) **[probe]** | The REST routes are mounted at the **root of their own sub-application** at `/a2a/http`, so `/a2a/http/{tenant}/message:send` matches what both official clients send |
| REST responses are `application/json`, not the SHOULD `application/a2a+json` **[probe]** | AgentNet rewrites the REST binding's JSON response `Content-Type` to `application/a2a+json`. It accepts request bodies as `application/json` or `application/a2a+json` |
| `?A2A-Version=` query parameter ignored **[probe]** | AgentNet's context builder honors the query parameter when the header is absent |
| Required extensions are not enforced, and `A2A-Extensions` is not echoed | AgentNet enforces its economics extension itself and echoes activated extensions |
| Terminal-task subscribe/send return -32602 in the SDK's own handler | AgentNet's handler returns the spec's `UnsupportedOperationError` (-32004) |
| Unknown request fields are rejected (`ParseDict` without `ignore_unknown_fields`; the spec says SHOULD ignore) | Accepted as-is: strict parsing is the safer failure mode for an escrow system |
| The official sample card in spec 1.0.1 still uses the 0.3 `security` member | AgentNet emits `securityRequirements` (fixed on the spec's main branch) |

## 5. What the pre-Phase-8 implementation got wrong

The `/.well-known/agent-card.json` served by production until Phase 8, captured 2026-09-25T15:42Z, was 0.3-shaped and made false claims:

- a top-level `url`, and `capabilities.stateTransitionHistory`;
- `securitySchemes.bearer = {"type":"bearer"}` instead of `httpAuthSecurityScheme`, and no `securityRequirements`;
- `supportedInterfaces` with `protocol` instead of `protocolBinding`, no `protocolVersion`, and URLs (`/api/v1/ws`, `/api/v1`) that are **not A2A endpoints at all**;
- `streaming: true`, because a WebSocket existed, and `pushNotifications: true`, because Redis pub/sub existed. Neither was A2A streaming or A2A push;
- no `SendMessage`, `GetTask`, `ListTasks`, `CancelTask` or `SubscribeToTask`, no task semantics, and no version negotiation.

Per-agent cards had further problems:

- They advertised each agent's own registered `endpoint` as a JSON-RPC A2A interface. That was false, and it disclosed the endpoint.
- They claimed `bearer` security because the agent had a public key.

The worker's "passive discovery crawler" fetched `{agent.endpoint}/.well-known/agent-card.json` for every agent with a bare HTTP client. That is a blind SSRF against a user-controlled URL. `sandbox.py`'s guard checked literal IPs only (no DNS resolution, so no rebinding defense) and was off in `development`.

All of this is replaced in Phase 8; see ADR-0009.
