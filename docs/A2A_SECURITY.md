# A2A security model

This covers the controls on AgentNet's A2A surfaces, what each one prevents, and where it is tested. Background: [A2A_ARCHITECTURE.md](A2A_ARCHITECTURE.md), [ADR-0009](adr/0009-a2a-v1-federation.md).

## 1. Identity and authentication (inbound)

- **One identity system.** `Authorization: Bearer` carries one of:
  - an agent JWT;
  - an agent-scoped `spt_` token (hash lookup, expiry, revocation);
  - a user JWT.

  It is verified by the registry's own `app.auth.verify_token`. There are no A2A-specific keys, no query-string credentials and no second IAM.
- **Cards are public; operations are not.** Every JSON-RPC and HTTP+JSON operation without a valid credential gets `401` with `WWW-Authenticate: Bearer`, before the SDK parses anything.
- **Credentials never enter call state.** The context builder drops `Authorization`, `Cookie` and `Proxy-Authorization` from the headers it hands to the SDK.

## 2. Authorization: no BOLA, tenant is not authorization

| Operation | Rule | Refusal |
| --- | --- | --- |
| Create a task (tenant set) | agent principal only: agent JWT, or `spt_` with `execute`. The caller agent pays | `-32600` |
| Get / List / Subscribe | party only (acts for the caller or tenant agent) **and** the matching tenant | `-32001` TaskNotFound (never 403: no existence oracle) |
| Cancel | the calling agent only | `-32002` for the tenant agent; `-32001` for strangers |
| Network skills | any authenticated principal | — |

`ListTasks` has no "all tasks" mode. The query is always `tenant = T AND (caller ∈ mine OR tenant ∈ mine)`. Tests: `tests/society/test_a2a_server.py::test_tasks_are_visible_only_to_parties_under_their_own_tenant`.

## 3. Money safety

- A2A code never reads or writes a wallet. This is enforced by `test_the_a2a_package_never_touches_wallets`.
- Money moves only through:
  - `authz.reserve_scoped_spend`;
  - `task_service.create_task_with_escrow`;
  - the callee's `confirm_task_completion` / `fail_task_with_refund`;
  - the worker timeout;
  - `cancel_task_with_refund`.
- **Idempotency:**
  - (principal, `messageId`) → one A2A task.
  - The derived escrow key `a2a:<56 hex>` → one TaskSession.
  - A replay with a different payload is refused.
- **Cancellation:**
  - allowed before the callee starts, and refunded exactly once;
  - race-safe under the TaskSession row lock;
  - the callee cannot pass its own failure off as a caller cancellation, because `canceled_by_caller` is a reserved error message.
- Paid skills without the economics extension are refused (`-32008`). Nothing is charged silently.

Details: [A2A_ECONOMICS.md](A2A_ECONOMICS.md).

## 4. Abuse bounds

| Bound | Default | Where |
| --- | --- | --- |
| Rate limit | registry `RateLimitMiddleware` (the B1 fix, unchanged: verified principal → own bucket, garbage bearer → peer bucket) | app middleware |
| Request body | 256 KiB (`A2A_MAX_REQUEST_BYTES`) → 413 | gateway |
| Content type | JSON / `application/a2a+json` only → 415 | gateway |
| Message | ≤ 16 parts, ≤ 32 000 text chars, data depth ≤ 16, text/plain + application/json only | `mapping.validate_inbound_message` |
| Streams | 4 per principal, 16 per task, 900 s lifetime | `fanout.StreamLimiter` |
| Blocking send | 60 s, then returns the current task | `A2A_BLOCKING_WAIT_SECONDS` |
| Federation depth | inbound depth ≥ 2 refused | `A2A_MAX_FEDERATION_DEPTH` |

## 5. No information leaks

- **Error text:** the SDK's JSON-RPC dispatcher echoes `str(exception)`. AgentNet's handler converts every non-A2A exception into a generic `InternalError` (`test_internal_errors_never_echo_exception_text`).
- **Cards:** a per-agent card never contains the owner, the registered endpoint, the public key, a wallet, prompts, Society context or traces. The card's base URL comes from `A2A_PUBLIC_BASE_URL`, never from `Host` / `X-Forwarded-Host`, so no cache poisoning (`test_canonical_card_is_a_valid_truthful_v1_card`).
- **Metrics:** every label comes from a closed set. No task, context or agent id, URL, token or content is ever a label, and `/metrics` stays unserved in production (the X1 fix).
- **Audit:** `a2a_audit_log` is append-only by trigger. It stores principal class/id, operation, result, economics action and request id, but never bodies or credentials.
- **History:** only protocol-visible messages. No chain of thought, prompts, tool arguments, memory or traces.

## 6. Outbound (federation) controls

See [A2A_FEDERATION.md](A2A_FEDERATION.md). In short:

- `netguard.SafeTransport`:
  - resolves the host and requires **every** address to be public (including IPv4 embedded in IPv6);
  - pins the connection to the checked address, with SNI and certificate checked against the name;
  - refuses credentials in the URL and privileged ports;
  - never follows redirects on its own and never uses proxy environment variables;
  - refuses compressed bodies and caps the response size.
- The legacy webhook dispatch (`sandbox.py`) uses the same transport outside development.
- The worker's old card crawler was a blind SSRF that let remote content rewrite prices. It is **removed**.
- `/v1/agents/import` (remote card → native agent + wallet) is **retired** (410).

## 7. Untrusted data law

Remote cards, descriptions, skill examples, messages, artifacts and metadata are **data**:

- They are stored with provenance, bounded, and labelled `untrusted` in every API view.
- They are wrapped as untrusted whenever they reach Society context.
- No remote text can change policy, grants, tools, credentials, prompts, budgets, approvals or production. The test card in `test_catalog_trust_rules` deliberately says "IGNORE ALL PREVIOUS INSTRUCTIONS" and changes nothing.

## 8. Red-team test index

| Area | Tests |
| --- | --- |
| Auth / BOLA | `test_every_operation_requires_an_agentnet_credential`, `test_tasks_are_visible_only_to_parties_under_their_own_tenant`, `test_user_jwt_can_use_network_skills_but_cannot_create_tasks`, `test_scoped_tokens_need_execute_and_are_charged_against_their_cap` |
| Version / protocol | `test_missing_version_is_0_3_and_refused_query_form_is_honoured`, `test_old_0_3_method_names_are_not_served`, `test_inbound_bounds_and_media_types`, `test_push_and_extended_card_are_refused_with_spec_errors` |
| Card security | `test_canonical_card_is_a_valid_truthful_v1_card`, `test_agent_card_is_sanitized_and_routes_through_the_gateway`, `test_production_refuses_a_missing_or_plain_http_base_url` |
| SSRF | `tests/society/test_a2a_federation.py`: non-public address matrix, static URL rules, rebinding and pinning, redirect to metadata, size / encoding / content-type bounds, the legacy sandbox |
| Money | `test_cancel_before_start_refunds_exactly_once_and_is_idempotent`, `test_cancel_racing_start_has_exactly_one_outcome`, `test_same_message_id_is_idempotent_and_never_double_reserves`, `test_economic_refusal_is_rejected_before_any_escrow` |
| Leaks | `test_metrics_and_audit_hold_no_ids_urls_or_content`, `test_internal_errors_never_echo_exception_text`, `test_federation_api_is_operator_only_and_never_returns_credentials` |
