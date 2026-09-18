# ADR-0004 — Self-developing society: repository intelligence, trusted risk tiers, promotion controller, fitness evaluation

- Status: accepted (Phase 3, 2026-09-18)
- Builds on: ADR-0001 (runtime), ADR-0002 (Phase 2 hardening), ADR-0003 (pre-live hardening)
- Code: `services/registry/app/society/{repo_intel,risk,promotion,promotion_github,fitness,deployment,router,telemetry}.py`, migration `0010_self_development`

## Context

After Phase 2.6 the Autonomous Society Runtime could produce a bounded *documentation* candidate in an isolated
worktree, and nothing else: the Builder could not read source code, could not touch core surfaces, could not
publish a branch, and no evaluation decided whether a change *improved* anything. Four historical boundaries
were hard walls. This ADR turns them into controlled gates without weakening the prime directive: **the model
is never the authority; it only produces typed requests, and trusted deterministic code decides.**

## Decisions

### D1 — Repository intelligence is a typed, read-only, bounded, audited intent family

`LIST_REPO_TREE`, `SEARCH_REPO`, `READ_REPO_FILE`, `READ_REPO_RANGE`, `READ_DIFF`, `READ_CANDIDATE_STATE`
(`repo_intel.py`) are the ONLY way an agent sees code. No shell, no MCP, no URL fetch. Paths are normalised
and contained after symlink resolution; `.git`, `.env*`, keys, secret-bearing and generated credential files are
refused (`risk.NEVER_READ_PATTERNS`); binaries are refused; every result is bounded in bytes/lines/entries by
settings, never by the caller; results are wrapped as `_untrusted` DATA. Each read is an `agent_intents` row
with its result, so the audit question "what did the Builder inspect?" is answerable from persisted state.

Loop protection: `SOCIETY_MAX_REPO_READS_PER_RUN`, `..._PER_CORRELATION`, `SOCIETY_MAX_REPO_BYTES_PER_RUN`,
`SOCIETY_MAX_ENGINEERING_TURNS` (per agent per correlation), `SOCIETY_MAX_CORRELATION_ENGINEERING_TURNS`,
`SOCIETY_MAX_ENGINEERING_CORRELATION_DEPTH`; identical searches inside a correlation are answered from the earlier
result without waking a new turn. All fail closed.

### D2 — The engineering loop is iterative through durable events, not a model-controlled shell

A run's read intents produce ONE targeted `repo.read.result` event (idempotent per run) that wakes the same agent
for its next turn; the context builder exposes that agent's previous read results (`repo_reads`) and the
engineering bounds (`engineering`). Writing still happens only through `SUBMIT_CODE_CANDIDATE` into the isolated
worktree under the `files_allowed` allow-list. Architect reconnaissance uses the same mechanism (search first,
design second) and derives `files_allowed`/`acceptance_tests` from deterministic search hits, never from a guessed
path (`tests/society/test_e2e_self_development.py` proves the file is found without being named).

### D3 — Risk is a trusted, path/diff-based four-tier classifier evaluated from the BASE revision

`risk.py`: GREEN (docs, templates, harmless tests) / AMBER (ordinary backend, SDK, examples) / RED (society
runtime, cognition/policy/risk/fitness/promotion, auth/authz, payment/wallet/escrow, sandbox, deployment, CI,
migrations, dependency and security policy, model routing/budgets) / NEVER (secret values, secret stores, git
internals, disabled tests, disabled CI gates, unrestricted shell, credential references outside the boundary).
Agents MAY propose RED changes (the old write deny-list shrank to NEVER-writable paths); RED is always
independent-Security + human-approved and never auto-merged. **Meta-change invariant:** the promotion
controller classifies a candidate with the `risk.py` it imported from the running revision; a candidate that
edits `risk.py`/`policy.py`/`fitness.py` in its worktree is still classified by the base copy
(`tests/society/test_risk_and_meta_change.py`). `code_candidates.risk_tier` is written only by that controller.

### D4 — Promotion is a separate, deterministic, lease-based controller with a provider abstraction

`promotion.py` owns the durable `code_promotions` record and its legal transitions
(REQUESTED → VALIDATING → BRANCH_READY → PR_OPEN → CI_PENDING → CI_PASSED → AWAITING_APPROVAL → MERGE_ELIGIBLE →
MERGED; REJECTED / SUPERSEDED / CI_FAILED / BLOCKED_EXTERNAL). The model can only emit `REQUEST_PR_PROMOTION`
(and the Builder never for its own candidate). Validation re-checks candidate READY, exact-head QA PASS,
Security PASS where the tier requires it, base ancestry, NEVER findings, prohibited files, change budgets and
independence, then publishes through a `PromotionProvider`: `DisabledPromotionProvider` (BLOCKED_EXTERNAL,
never fake success), `FakePromotionProvider` (shadow promotion in tests/demo) and `GitHubPromotionProvider`
(inert without `SOCIETY_GITHUB_REPOSITORY` + `SOCIETY_GITHUB_TOKEN`). The base branch is refused everywhere,
the interface has no force option, branch names are deterministic (`agentnet-auto/<candidate-id>`), PRs are
looked up by head branch before creation, and every transition is persisted before the next side effect so a
crashed controller converges without a duplicate branch/PR (`tests/society/test_promotion.py`).

### D5 — Merge eligibility is computed from persisted facts; auto-merge is OFF and GREEN-only

`compute_eligibility` requires trusted tier promotable, CI passed on the exact head, branch up to date, QA
PASS, Security PASS where required, no critical scanner finding, fitness experiment PASS, no unresolved
review, change budget, and human approval (AMBER/RED always; GREEN whenever `SOCIETY_AUTO_MERGE_ENABLED` is
false — the default). Config validation refuses `SOCIETY_AUTO_MERGE_ENABLED=true` with the GitHub provider in
this phase (Level 3 is not exercised). Human merges are observed, never performed, by the controller.

### D6 — Fitness is hard gates + multi-objective deltas under a snapshotted trusted criteria set

`fitness.py` runs an offline experiment (ephemeral base worktree vs candidate worktree, same pytest targets,
scrubbed env, timeouts) and records baseline/candidate metrics across correctness, reliability, safety,
economics, performance and autonomy quality on `change_experiments`. Hard gates (no test regression, no test
removal, no security regression, no NEVER finding, metric collection not disabled, tests completed, tier not
NEVER) are evaluated first; soft deltas only when every gate passes; decision PASS / FAIL / INCONCLUSIVE with
confidence. `TRUSTED_CRITERIA` is snapshotted onto the row at request time from the running module — a
candidate that rewrites thresholds is judged by the pre-change criteria (`tests/society/test_fitness.py`).
`evaluation_mode='offline'`; real-traffic metrics are never fabricated. A failing post-merge experiment sets
`rollback_recommended` and emits `rollback.recommended` with the previous known-good SHA; no deploy is executed.

### D7 — Evaluator role, memory provenance, telemetry, routing and change budgets

`Society_Evaluator` (independent of Builder/QA/Security) may only request an experiment, record an advisory
recommendation on a finished experiment and write memory. Memory rows carry provenance (`source_type`,
`source_id`, `correlation_id`, `author_agent_id`, `confidence`, `validation_state`, `expires_at`,
`superseded_by`) set by trusted code; retrieval ranks by importance, confidence, recency (half-life by
validation state) and supersession without deleting anything; poisoned memory is data, never policy
(`tests/society/test_memory_provenance.py`). `telemetry.py` turns persisted metrics into
`platform.metric.anomaly` events with threshold + cooldown + idempotency and evidence fields; a Scout proposal
driven by a world signal must carry `evidence`. `router.py` maps logical tiers (`fast`/`strong`) to configured
model names and fails closed on budget; `code_candidates.diff_hash` and the change-budget settings implement
anti-busywork (no-op, format-only, duplicate, unlinked, oversize).

### D8 — Deployment is an interface with durable state and no host

`deployment.py`: `DeploymentProvider` with Disabled/Fake implementations and `deployment_requests` rows.
Without a provider a request is `BLOCKED_EXTERNAL`; a production request is recorded as `REFUSED` and no
executor exists for it. No Vercel/Railway/Render/Fly/Cloud Run code was added.

## Official documentation consulted (verified 2026-09-18)

`docs.github.com`, `api-docs.deepseek.com` and `genai.owasp.org` are blocked by this environment's egress
policy; GitHub facts were read from the docs source in the `github/docs` repository (raw markdown of the same
pages) and DeepSeek/OWASP facts from search excerpts of the official pages. Residual uncertainty is noted.

| Topic | Source | Fact recorded |
| --- | --- | --- |
| GitHub App permissions | `content/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app.md` | "If your app specifically needs to access or edit Actions files in the `.github/workflows` directory, request the 'Workflows' repository permission"; HTTP git access needs "Contents". Administration is called out as a high-level permission to justify explicitly. |
| Installation access tokens | `.../generating-an-installation-access-token-for-a-github-app.md` + reusable | JWT → `POST /app/installations/{id}/access_tokens`; optional `repositories`/`permissions` body parameters scope the token down; "The installation access token will expire after 1 hour"; stateless `ghs_APPID_JWT` format rolling out since 2026-04-27 (never assume 40 chars). |
| GITHUB_TOKEN | `content/actions/concepts/security/github_token.md` | It is an installation token limited to the repository; expires at job end; events it creates do not trigger new workflow runs except `workflow_dispatch`/`repository_dispatch`, and workflow-created PRs run in an approval-required state. Consequence: the future Society App must use its own installation token so PR CI runs. |
| Workflow permissions | `content/actions/reference/workflows-and-actions/workflow-syntax.md#permissions` | `permissions:` at workflow/job level; CI keeps `contents: read`. |
| Environments | `content/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments.md` | Required reviewers, wait timer, "Allow administrators to bypass configured protection rules" toggle, custom protection rules via GitHub Apps — the future staging gate. |
| Rulesets | `content/repositories/.../available-rules-for-rulesets.md` | Rules: Require a pull request before merging (approvals, dismiss stale, code owners, last-push approval, resolved conversations, merge type), Require status checks to pass (strict = "Require branches to be up to date before merging", expected check source = a specific app with `statuses:write`), Block force pushes (default on), Restrict deletions (default on), Require linear history; bypass actors may include GitHub Apps — the Society App must NOT be a bypass actor. |
| DeepSeek API | official docs via search excerpts (`api-docs.deepseek.com`, change log) | OpenAI-compatible `/chat/completions` at `https://api.deepseek.com`; current recommended model id `deepseek-flash` (V4.1-Flash; `deepseek-v4-flash` accepted as legacy) and `deepseek-v4-pro` continues; legacy `deepseek-chat`/`deepseek-reasoner` retired 2026-07-24; JSON Output = `response_format={"type":"json_object"}` **and** the word "json" plus an example in the prompt, sensible `max_tokens`, documented occasional EMPTY content; a Responses API exists for Flash. `json_schema` is not documented → never assumed (`SOCIETY_MODEL_OUTPUT_FORMAT=auto` probes once and remembers). Model ids stay configuration (`SOCIETY_MODEL_FAST_NAME`/`_STRONG_NAME`). Uncertainty: exact pricing/limits not verified here. |
| OWASP Top 10 for Agentic Applications 2026 | announced 2025-12-09, via excerpts | ASI01 goal hijack (prompt injection is data-only, typed intents), ASI02 tool misuse (typed read ops, no shell), ASI03 identity & privilege abuse (grants only from seed; model never holds tokens), ASI04 supply chain (dependency policy is RED), ASI05 unexpected code execution (worktree + argv-only subprocess + NEVER shell), ASI06 memory poisoning (provenance, decay, memory never policy), ASI07 inter-agent comms (untrusted wrapping), ASI08 cascading failures (loop breakers, budgets, leases), ASI09 human-agent trust (human approval on AMBER/RED, facts-only PR bodies), ASI10 rogue agents (circuit breaker, kill switches, change budgets). |

## Consequences

- Levels 1–4 of the self-development maturity model are implemented and proven with fakes; none is CLAIMED
  live (no model credential, no GitHub App, no host).
- Two more RED surfaces exist (`promotion*.py`, `fitness.py`); they are covered by the meta-change tests.
- `main` protection in the repository is still an owner action (no ruleset existed at authoring); the code
  refuses to publish to the base branch regardless (docs/GITHUB_PROMOTION.md lists the exact ruleset).
