# ADR-0007 — DeepSeek live-provider compatibility (Phase 4.1)

Status: accepted (2026-09-19) · Refines ADR-0002 (live-model preflight and canaries) and ADR-0004 D-model
(DeepSeek-compatible output negotiation). Scope: provider compatibility only — no Society role, risk tier,
promotion, deployment or DNS change.

## Context

The first live preflight from Railway staging (2026-09-19 02:16 UTC, credential present only in the
`society-worker` service, credential safety scan PASS) returned

```
LIVE MODEL BLOCKED — PROVIDER UNREACHABLE
probe.error = "provider did not return the requested JSON object"
```

The provider had been reached and had answered. Two things were wrong at once:

1. **The probe request.** It asked for `response_format={"type":"json_object"}` with `max_tokens=20` and no
   reasoning control. DeepSeek's current API thinks by default (effort `high`); reasoning is generated
   before the answer and consumes the output budget, so a 20-token budget is spent on `reasoning_content`
   and `message.content` comes back empty or cut off (`finish_reason="length"`).
2. **The diagnosis.** Every non-`{"ok": true}` outcome collapsed into "unreachable", which is false for a
   provider that returned HTTP 200, and the probe was built separately from the runtime request path, so
   a probe could pass while `OpenAICompatibleModel.decide()` would still fail (or the reverse).

## Official DeepSeek contract (verified 2026-09-19)

Sources: `api-docs.deepseek.com` — *Thinking Mode* (`/guides/thinking_mode`), *JSON Output*
(`/guides/json_mode`), *Chat Completions API* (`/api/create-chat-completion`), *Models & Pricing*
(`/quick_start/pricing`), *Change Log* (`/updates`), the V4.1-Flash release note (`/news/news260910`). The
docs host is egress-blocked from the engineering session; the statements below were read through search
snippets of those pages and must be re-checked against the pages themselves when the contract next changes.

| Item | Documented behaviour |
| --- | --- |
| Endpoint | OpenAI-compatible `https://api.deepseek.com` (`/chat/completions`; also `…/v1`) |
| Model | `deepseek-flash` (served by DeepSeek-V4.1-Flash). Legacy `deepseek-v4-flash` still accepted and billed at the Flash price. `deepseek-v4-pro` is being phased out: since 2026-09-14 04:00 UTC its requests route to V4.1-Flash until V4.1-Pro launches. Phase 4.1 uses `deepseek-flash` only |
| Context / pricing | 1M-token context by default; Flash: $0.14 / 1M input (cache miss), $0.0028 / 1M input (cache hit), $0.28 / 1M output |
| Thinking default | **enabled**, default effort **high** |
| Thinking toggle | request field `thinking: {"type": "enabled" \| "disabled"}`; with the OpenAI SDK via `extra_body` |
| `reasoning_effort` | `none` disables thinking; `low` / `high` / `max` enable it (no `medium`). Default `high` |
| Reasoning output | in thinking mode the chain of thought is returned as `message.reasoning_content`, a sibling of `content`; `usage.completion_tokens_details.reasoning_tokens` reports its size (reasoning is billed as output). With `tools`, `reasoning_content` must be passed back in later turns (400 otherwise) — AgentNet sends no tools and is single-turn |
| Ignored in thinking mode | `temperature`, `presence_penalty`, `frequency_penalty` (accepted, no effect); `top_p` floors at 0.95 |
| JSON output | `response_format: {"type": "json_object"}`; the prompt must contain the word `json` and an example of the shape; set `max_tokens` "reasonably" to avoid truncation; the API "may occasionally return empty content", mitigated through the prompt |
| `finish_reason` | `stop`, `length` (max_tokens reached), `content_filter`, `tool_calls`, `insufficient_system_resource`, `aborted` |
| Errors | 400 invalid format, 401 authentication fails, 402 insufficient balance, 422 invalid parameters, 429 rate limit, 500 server error, 503 server overloaded |

## Decisions

### D1 — One request-capability layer, shared by preflight and runtime

`RequestPolicy` (`services/registry/app/society/cognition.py`) turns three provider-neutral settings into
the wire fields the configured profile documents, and `OpenAICompatibleModel.build_chat_request()` is the
only place a chat request is assembled. `decide()` and the preflight probe both call it, and both consume
the answer through `OpenAICompatibleModel.complete_json()` — the same bounded request-retry loop and the
same bounded empty-content handling. The probe can no longer pass on a request shape the runtime does
not send.

### D2 — Explicit, provider-safe reasoning configuration

| Setting | Values | Meaning |
| --- | --- | --- |
| `SOCIETY_MODEL_CAPABILITY_PROFILE` | `generic` (default) \| `deepseek` | which provider-specific fields may be sent at all |
| `SOCIETY_MODEL_THINKING_MODE` | `auto` (default) \| `disabled` \| `enabled` | `auto` sends nothing (provider default); `deepseek` profile → `thinking={"type": …}` |
| `SOCIETY_MODEL_REASONING_EFFORT` | `auto` (default) \| `none` \| `low` \| `medium` \| `high` \| `max` | `auto` sends nothing; otherwise `reasoning_effort` is sent. The `deepseek` profile refuses `medium` (undocumented) |

Rules: the `generic` profile never sends a `thinking` field (a plain OpenAI-compatible endpoint would
reject an unknown parameter); an explicit `reasoning_effort` is passed through on any profile because the
operator asked for it; invalid or contradictory values (`disabled` + `high`, `enabled` + `none`) raise
`SocietyConfigError` at startup instead of being coerced. No model-name sniffing anywhere
(`"deepseek" in model_name` is not a mechanism). Defaults keep every existing deployment byte-identical on
the wire.

### D3 — Initial live posture: structured actions over hidden reasoning

Railway `society-worker` (non-secret variables only): `SOCIETY_MODEL_CAPABILITY_PROFILE=deepseek`,
`SOCIETY_MODEL_THINKING_MODE=disabled`, `SOCIETY_MODEL_REASONING_EFFORT=none`,
`SOCIETY_MODEL_OUTPUT_FORMAT=json_object`, model `deepseek-flash`. Phase 4.1 proves
connectivity → structured JSON → typed `AgentDecision` → Society mechanics; reasoning quality is a later
concern and is never traded for reliability of the typed contract.

### D4 — The probe is a minimal connectivity + structured-output check

`PROBE_SYSTEM_PROMPT` / `PROBE_USER_PROMPT` (the word `json` plus the example `{"ok": true}`), `temperature`
0, `response_format=json_object`, `max_tokens = PROBE_MAX_TOKENS = 256` (never 20 again), and exactly the
runtime's reasoning policy. The probe is identified by its prompts, not by a token constant (tests included).
The probe does not silently override the runtime policy: if the runtime is left on `auto` against DeepSeek,
the probe fails precisely (`output_truncated` / `empty_content`) with the remedy in `probe.hint` — a green
probe means the runtime request shape works.

### D5 — Precise, redacted diagnostics; a reachable provider is never "unreachable"

`ProbeResult.category` ∈ `ready`, `misconfigured`, `provider_unreachable`, `authentication_failed`,
`rate_limited`, `provider_error`, `empty_content`, `output_truncated`, `output_contract_failed`. Public
verdicts: `LIVE MODEL READY`; `LIVE MODEL BLOCKED — PROVIDER UNREACHABLE` (transport / timeout / no base
URL); **new** `LIVE MODEL BLOCKED — PROVIDER ERROR` (401/403, 429 after bounded retries, 402/4xx/5xx);
**new** `LIVE MODEL BLOCKED — OUTPUT CONTRACT` (empty, truncated, malformed or semantically wrong content).
`NO SAFE CREDENTIAL` and `PROVIDER IS NOT LIVE` are unchanged. Exit codes are unchanged (0 ready, 3
otherwise), so existing automation keeps working.

Safe structural metadata only: HTTP status category, `finish_reason`, `content_present`,
`reasoning_present`, `reasoning_tokens`, response format, model, requests, retries, empty-content retries,
format fallbacks, latency, token counts, capability profile, thinking mode, effort, request field names,
an 8-hex credential fingerprint prefix. Never: content, reasoning text, prompts, headers, the key, full
provider bodies.

### D6 — No chain of thought, anywhere

`reasoning_content` is inspected for emptiness and its token count only. It is never returned from the
adapter, never placed in `ModelResponse.raw_summary`, `AgentRun`, memory, logs, exceptions or the
`negotiation_log`, never exposed through JARVIS/API, and never parsed for JSON or intents. Only
`reasoning_present: bool` and `reasoning_tokens: int` may be kept. Regression tests plant instruction-like
text and JSON in a fixture reasoning field and prove none of it survives.

### D7 — Bounded empty-content handling, once

`complete_json()` retries an empty `content` at most `SOCIETY_MODEL_EMPTY_CONTENT_RETRIES` times, counts
the retries performed (`empty_retries`), accumulates usage across attempts, and raises `EmptyContentError`
(a `DecisionValidationError` carrying `finish_reason`, `reasoning_present`, `empty_retries`) when exhausted.
Empty-content retries are separate from the transport/429/5xx budget and never trigger side effects
(cognition has none). Malformed or semantically wrong content is never retried.

### D8 — Cost accounting unchanged

Usage comes from the provider's `usage` block: `tokens_out = completion_tokens`, which already includes
reasoning tokens, so `reasoning_tokens` is reported but never added again. Daily, correlation and role
budgets are untouched.

## Consequences

* `python -m app.society.canary preflight` now distinguishes a provider that could not be reached from one
  that rejected the request or answered outside the contract; the runbook table lists all six verdicts.
* Contract surfaces updated in the same change: `config.py`, `.env.example`, `docker-compose.staging.yml`,
  `.railway/railway.ts` (provider-neutral defaults), `docs/DEPLOYMENT_ARCHITECTURE.md` §2,
  `docs/SOCIETY_LIVE_MODEL_RUNBOOK.md` §2, `CURRENT_STATE.md`; tests in
  `tests/society/test_deepseek_live_compat.py` (cases A–K, capability profiles, config fail-fast, runtime
  request shape, no-CoT, accounting) plus the updated `test_canary.py`.
* Not done here: `deepseek-v4-pro` / V4.1-Pro, tool calls, multi-turn reasoning pass-back, prompt tuning
  for reasoning quality, any Society activation.
