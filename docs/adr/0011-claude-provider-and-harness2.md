# ADR-0011: Claude as the live provider, and recursive harnessing (Harness²) for the Builder

Status: accepted (2026-10-24). Both parts are behind configuration; nothing changes until an operator sets them.

## Context

1. The Society, the maintenance activities and the bench ran on DeepSeek through the OpenAI-compatible adapter
   (ADR-0007). The owner wants Claude (Anthropic API, owner-held credits) for daily runs.
2. Harness² ("Recursive Agent Harnessing for an Open World", arXiv 2610.14224, Apache-2.0) improves the system
   around a frozen model at two levels: a task-level contrastive *Propose-Probe-Compose* step without a
   ground-truth verifier, and a persistent *improvement harness* of adopted harness edits plus lessons kept only
   when execution contrasts support them. The Builder's coding harness already had best-of-N sampling whose only
   diversity was temperature -- which the Claude Messages API no longer accepts.

## Decision 1 -- `SOCIETY_MODEL_PROVIDER=anthropic`

* `cognition.AnthropicModel` calls the official `anthropic` SDK (pinned `2.3.0`, Messages API). It subclasses the
  OpenAI-compatible adapter and translates at the wire, so the bounded request retries, the empty-content retries,
  strict decision parsing, the typed truncation failure and the accounting are ONE code path for both providers.
  `cognition.live_model()` is the single factory: runtime, maintenance activities, preflight probe, canary and the
  bench relay all use it.
* Request: the system prompt is one `cache_control` block (a role's prompt + schema doc repeat across runs); no
  `temperature` (refused by the API since version `2026-10-21`); no prefill; reasoning is adaptive thinking with
  depth from `SOCIETY_MODEL_REASONING_EFFORT` (`output_config.effort`, `low..max`). `SOCIETY_MODEL_THINKING_MODE=disabled`
  is refused for Claude (current models always think), so is `effort=none`.
* Reply: text blocks are the content; thinking blocks are a presence flag (text never kept); ONE markdown fence
  around the whole reply is framing and removed -- prose around JSON is still rejected. `stop_reason=refusal` raises
  `ModelRefused` (never replayed); `SOCIETY_MODEL_REFUSAL_FALLBACK` (default on) lets the API re-run a declined
  request on a fallback model in the same call (`fallbacks: "default"`, Opus/Sonnet/Fable only).
* Money (model spend, never wallets): known Claude models are priced from a built-in list-price table
  (`config.ANTHROPIC_USD_PER_1K`), by the model that actually answered; every input token, cached or not, is charged
  at the input rate, so the daily budget is conservative.
* DeepSeek leftovers fail fast with the fix in the message (profile `deepseek`, `THINKING_MODE=disabled`,
  `REASONING_EFFORT=none`, a DeepSeek base URL). `LIVE_PROVIDERS = (openai_compatible, anthropic)` is what counts as
  live for NO FAKE AUTONOMY (canary, staging validators).

## Decision 2 -- Harness² for AuthorPatch (`maintenance/harness2.py`, `MAINTENANCE_HARNESS2_ENABLED`, default off)

* Task level: reference run on the base harness; if it does not deliver, one editor call proposes an *invariant*
  and a *domain* guidance edit; each is probed on the same task from the same base; edits whose behavioural contrast
  with the reference is `improved` are composed and run once. First DELIVERED run wins -- delivery is the harness's
  own acceptance gate, never the editor or a contrast. All runs and the editor share the caller's cost cap.
* An edit is advisory text in the AuthorPatch input (`harness_guidance`): it cannot change tools, `files_allowed`,
  tests, budgets or the system-prompt laws.
* Harnessing level: `society_harness_lessons` (schema `SOCIETY_HARNESS_SQL`, migration `0021_harness_lessons`). An
  edit becomes a lesson only when its run delivered where the reference did not. Later tasks still start from the
  base harness; only the editor reads the library (paper: "the improver starts with the accumulated library").
  Reused lessons gain support or contradiction from what their probe did; a lesson contradicted more than once
  beyond its support retires. Evidence is structural (outcome classes and counts).
* Scope: the Society Builder (`BUILD_CODE_CANDIDATE` via `build_engine.build`) and the bench's `--path society`.
  The maintenance kernel's AuthorPatch keeps best-of-N until the library has live evidence.

## Consequences

* Railway (society worker): `SOCIETY_MODEL_PROVIDER=anthropic`, `SOCIETY_MODEL_API_KEY=<Claude key>`, unset the
  DeepSeek-only variables. See `docs/CLAUDE_PROVIDER.md`.
* `maintenance/harness2.py` sits in the RED trusted base with the rest of `maintenance/`; a Society candidate cannot
  edit the rules that judge its own harness.
