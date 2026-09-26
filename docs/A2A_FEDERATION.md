# A2A federation: AgentNet as a client

AgentNet can discover and call external A2A 1.0 agents. Remote agents are **vendors, never authorities**. Decision: [ADR-0009 D12–D14](adr/0009-a2a-v1-federation.md). Flag: `A2A_FEDERATION_ENABLED` (default `false`).

## 1. Catalog and trust states

`a2a_remote_agents` stores each remote agent's:

- card URL, host, provider;
- sanitized name, description and skills (bounded);
- protocol versions and bindings;
- security scheme summary;
- card hash and ETag;
- fetch and validation times, failure counters;
- provenance.

Every version is kept in `a2a_remote_card_versions`.

| State | Meaning | Who sets it |
| --- | --- | --- |
| `discovered` | the card was fetched and validated | fetcher |
| `verified` | AgentNet may call it | **operator only** |
| `degraded` | 3 consecutive fetch failures, or the card changed since verification | fetcher |
| `quarantined` | the card failed validation | fetcher |
| `blocked` | never contacted again | operator |

Rules:

- **Reachable never means trusted.** A card change can only lower trust.
- A `verified` agent whose card hash differs from the one the operator verified drops to `degraded`.
- A recovered agent returns to `verified` only if its card is byte-for-byte the verified one; otherwise it becomes `discovered`.

## 2. Safe fetching (SSRF)

`app/a2a/federation/netguard.py` + `fetcher.py`:

- `https` only in production; no credentials in the URL; no privileged ports other than 80/443.
- Resolves the host and requires **every** address to be public. Refused ranges:
  - loopback, RFC 1918, RFC 6598 shared space;
  - link-local / metadata, ULA, multicast, reserved, unspecified;
  - IPv4 embedded in IPv6 (mapped, NAT64, 6to4) is checked too.
- **Pins** the connection to the checked address (URL host → IP), with `Host` and TLS SNI set to the name. The certificate is verified against the name, so DNS rebinding cannot redirect the socket.
- At most 3 redirects, each fully revalidated. A redirect to a private address is refused.
- `Accept-Encoding: identity`; compressed responses are refused, so a decompression bomb cannot expand.
- 256 KiB body cap and 10 s timeout. Proxy environment variables are ignored (`trust_env=False`).
- JSON content type only.
- The card is parsed with the SDK's `AgentCard` type, then AgentNet's bounds apply:
  - at least one 1.x `JSONRPC` or `HTTP+JSON` interface, with a URL that also passes the static checks;
  - ≤ 128 skills.
- Every refusal is counted in `agentnet_a2a_ssrf_rejections_total{reason_class}`, a closed label set.

`A2A_FEDERATION_TEST_PRIVATE_HOSTS` exists only for local tests. It is ignored in production, whatever it says.

## 3. Connections and the credential vault

- `a2a_connections` links a remote agent to a label, an auth scheme (`none` | `bearer`) and a daily call limit.
- A bearer credential is stored only as a Fernet token (`A2A_CREDENTIAL_KEY`).
- The credential is **write-only**: no API response contains it or its sealed form.
- It is unsealed only inside the outbound client, used as one `Authorization` header, then dropped.
- It is never logged, never put in Society context and never forwarded to another agent.
- Revoking a connection destroys the sealed credential.

## 4. Outbound calls

`app/a2a/federation/client.py` uses the **official SDK client** (`ClientFactory`) over the SafeTransport.

- Allowed only to `verified` agents, through an active connection, within the daily limit and the federation depth.
- Every call is a durable `a2a_outbound_calls` row with an idempotency key. A repeated key returns the recorded call.
- **A send is never retried.** A pending row is claimed atomically (`pending → sent`). A crash after the claim marks the call failed ("interrupted, not retried") and never resends it.
- Reads (`GetTask`) retry once.
- Results are summarized, bounded and labelled `untrusted`.
- The SDK's card-resolver logging is lowered to WARNING, so remote content never goes to AgentNet logs.

## 5. Operator API (operator role, user JWT)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/v1/a2a/federation/agents` | catalog (labelled untrusted) |
| POST | `/v1/a2a/federation/agents` `{cardUrl}` | discover |
| POST | `/v1/a2a/federation/agents/{id}/refresh` | refresh + drift |
| POST | `/v1/a2a/federation/agents/{id}/state` `{state, reason}` | the only way to `verified` / `blocked` |
| GET/POST | `/v1/a2a/federation/connections` | create (credential write-only) / list |
| DELETE | `/v1/a2a/federation/connections/{id}` | revoke (destroys the credential) |
| GET/POST | `/v1/a2a/federation/calls` | outbound log / operator-initiated call |
| POST | `/v1/a2a/federation/calls/{id}/check` | GetTask on the remote task |

Public and structural only: `GET /v1/a2a/federation/summary` gives counts by state.

## 6. Society as a client (A2A intents)

| Intent | Risk | Who | Rule |
| --- | --- | --- | --- |
| `DISCOVER_A2A_AGENT {card_url, reason}` | MEDIUM | Governor | https URL on `A2A_SOCIETY_DISCOVERY_ALLOWED_HOSTS` only |
| `REFRESH_A2A_AGENT {remote_agent_id}` | LOW | Scout | catalog id only |
| `REQUEST_A2A_TASK {connection_id, skill_id, input, budget_class, reason}` | MEDIUM | Governor | **approval required**; the checks are listed below |
| `CHECK_A2A_TASK {outbound_call_id}` | LOW | Governor, Scout | reads AgentNet's own record; no network |

Checks on `REQUEST_A2A_TASK`:

- `verified` agent only;
- input ≤ 4000 bytes, with no credential-like keys;
- per-day and per-correlation caps (the agent-chain breaker);
- a per-remote circuit breaker.

Mechanics:

- The intents are policy-gated by `A2A_SOCIETY_CLIENT_ENABLED` and `A2A_FEDERATION_ENABLED`.
- Executors only record a request.
- The **federation pump** in the Society worker does the network work and emits:
  - `a2a.task.finished`;
  - `a2a.agent.discovered`;
  - `a2a.agent.refreshed`.

  Each event is causation-linked to the request event. Its payload is always wrapped as untrusted data in the model context.
- The model sees connection ids and labels, **never** a credential or a raw card.

## 7. Recursion

Inbound requests carrying `metadata["https://agentnet.io.vn/a2a/extensions/federation/v1"].depth ≥ A2A_MAX_FEDERATION_DEPTH` (default 2) are refused. Outbound calls carry `depth` and are refused above the same bound. The Society's per-correlation cap stops agent chains.
