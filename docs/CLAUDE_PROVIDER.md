# Running the Society on Claude

ADR-0011. The provider is configuration; switching back to DeepSeek is the same variables in reverse.

## Railway: society-worker service

Set:

| Variable | Value | Why |
|---|---|---|
| `SOCIETY_MODEL_PROVIDER` | `anthropic` | the Claude adapter (official `anthropic` SDK) |
| `SOCIETY_MODEL_API_KEY` | your Claude API key | replaces the DeepSeek key; never commit it |
| `SOCIETY_MODEL_NAME` | `claude-sonnet-5-5` | FAST tier: Scout, Governor, QA, Security, most decisions, and the coding/maintenance tool loops |
| `SOCIETY_MODEL_STRONG_NAME` | `claude-opus-5-5` | STRONG tier (router.py): Builder decisions on real code, Architect on RED work, Evaluator on ambiguous evidence, retries after invalid output |
| `SOCIETY_MODEL_REASONING_EFFORT` | `medium` | thinking depth; `high` for more quality, `low` to save money |
| `SOCIETY_MODEL_MAX_OUTPUT_TOKENS` | `16000` | thinking counts against it; 4000 truncates code candidates |
| `SOCIETY_MODEL_TIMEOUT_SECONDS` | `180` | a thinking model needs longer than 45 s on big contexts |
| `SOCIETY_RUN_LEASE_SECONDS` | `300` | longer than one model call |
| `MAINTENANCE_BUILDER_MAX_OUTPUT_TOKENS` | `8000` | per-turn budget of the coding loop (thinking included) |
| `SOCIETY_DAILY_MODEL_BUDGET` | your daily cap in USD | Sonnet costs more per token than DeepSeek; the cap still holds |

Delete (DeepSeek-only; the worker refuses to start with them and says which one):
`SOCIETY_MODEL_BASE_URL` (default is `https://api.anthropic.com`), `SOCIETY_MODEL_CAPABILITY_PROFILE`,
`SOCIETY_MODEL_THINKING_MODE` (when `disabled`), `SOCIETY_MODEL_REASONING_EFFORT=none`, `SOCIETY_MODEL_FAST_NAME`,
`SOCIETY_MODEL_USD_PER_1K_*` (known Claude models are priced from the built-in table).

On the registry service set only `SOCIETY_MODEL_PROVIDER=anthropic` (it mirrors the flag for `/v1/society/status`
and never holds the key).

## Verify before the first live run

```bash
python -m app.society.canary preflight      # inside the society-worker container
```

`LIVE MODEL READY` means the key is safe (not in git history), the endpoint answered a structured-output probe and the
request policy is what you configured. Any other verdict names the problem.

## What changes and what does not

* Same retries, parsing, budgets, policy and approvals: the adapter translates at the wire and reuses the existing loop.
* The system prompt is cached between runs of the same role (cheaper input).
* A declined request (`stop_reason=refusal`) is re-run once on a fallback model by the API
  (`SOCIETY_MODEL_REFUSAL_FALLBACK=true`), otherwise it fails the run without a retry.
* Optional, off by default: `MAINTENANCE_HARNESS2_ENABLED=true` turns on the Harness² Propose-Probe-Compose step for
  the Builder (ADR-0011, `maintenance/harness2.py`).
