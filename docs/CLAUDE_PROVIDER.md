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
* The system prompt is cached between runs of the same role (cheaper input). The conversation prefix is cached only
  for maintenance/build activities (multi-turn tool loops); a one-shot decide would pay the 1.25x write for nothing.
* Spend is priced with prompt caching: cache reads at 0.1x the input rate (0.05x on Opus 5.5, 0.025x on Fable 5.1),
  5-minute cache writes at 1.25x. A model missing from `ANTHROPIC_USD_PER_1K` keeps every input token at full rate.
* A declined request (`stop_reason=refusal`) is re-run once on a fallback model by the API
  (`SOCIETY_MODEL_REFUSAL_FALLBACK=true`), otherwise it fails the run without a retry.
* Optional, off by default: `MAINTENANCE_HARNESS2_ENABLED=true` turns on the Harness² Propose-Probe-Compose step for
  the Builder (ADR-0011, `maintenance/harness2.py`).

## Calibrating the caps from measured spend

The caps were sized for DeepSeek. Read what Claude actually costs, then set the caps from it:

```bash
PHASE5_PLAN=costs:7:6        # staging-validator: p50/p95 per role+model, per build, per activity, per day
```

`PHASE5-JSON costs.recommend` prints `MAINTENANCE_MAX_CASE_COST_USD`, `SOCIETY_MAX_CORRELATION_COST_USD`,
`SOCIETY_MODEL_STRONG_FLOOR_USD` and a grant budget per role, each from a printed rule
(`deploy/railway/phase5_live.py::recommend_costs`). It is a recommendation: the env vars are set by the owner, and a
role's grant budget by the operator command

```bash
PHASE5_PLAN=budget:builder:2.20
```

The deploy re-seed keeps the larger of an operator budget and the role default (as approval gates are unioned); no
intent can change a budget. `SOCIETY_MODEL_STRONG_FLOOR_USD` (default 0.05) is the headroom the strong tier needs in
both the agent and the story budget, so an Opus run is never started when it cannot finish inside them.

## Which runs use the strong model

The router (`society/router.py`) picks the tier; the model never does. A decide run on a candidate wake reads the
candidate's spec kind and trusted risk tier (the classifier over `files_allowed`), so a Builder decision on code and an
Architect decision on RED work route STRONG. The build itself (the AuthorPatch loop of `BUILD_CODE_CANDIDATE`, most of
a candidate's spend) has its own route, `choose_build_route`:

| Build | Tier |
|---|---|
| RED/NEVER files, a bench ticket, or a re-build after a failed build or QA run | STRONG |
| any other first build | FAST |

Escalation needs `SOCIETY_MODEL_STRONG_FLOOR_USD` of headroom in both the Builder's grant and the story budget, and a
build's cost cap is `MAINTENANCE_MAX_CASE_COST_USD` but never more than that headroom. The run records the build's
tier, model and reason (`decide reason | build reason (cap $x)`). To see the last routes and the build delivery rate:

```bash
PHASE5_PLAN=routes:20,costs:1:15   # routes.*: tier/model/reason per run; costs.builds: delivered, usd_per_delivered
```

With Harness² on, the reference build, the editor call and each probe share that one cap; a reference build that has
spent it ends the build without an editor call.
