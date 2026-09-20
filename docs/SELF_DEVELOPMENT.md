# Self-development — how AgentNet improves its own code under human control

Status (2026-09-18):

```
SELF-DEVELOPMENT MECHANICS: PROVEN
REAL SOURCE CODE CANDIDATE: PROVEN DETERMINISTICALLY
SHADOW PR PROMOTION: PROVEN
OFFLINE FITNESS: PROVEN
LIVE MODEL: NOT YET PROVEN
REAL SOCIETY GITHUB APP: NOT YET CONFIGURED
HOSTING: NOT DEPLOYED
A2A V1: NOT STARTED
LEGACY FILE BACKLOG: RETIRED FROM ACTIVE RUNTIME
SYNTHETIC POLL ACTIVITY: LEGACY/DEMO ONLY
GITHUB APP AUTH: IMPLEMENTED, NOT CONFIGURED
REAL GITHUB PROMOTION: NOT RUN
MAIN RULESET: OWNER ACTION REQUIRED
DEEPSEEK KEY: NOT PROVIDED
```

Phase 3.1 closed the pre-deploy boundaries (ADR-0005): the Society runtime is the ONLY autonomous improvement
control plane (the worker's reflection loop and `AGENT_BACKLOG.md` bridge are archived under `legacy/`), the
synthetic poll/echo/storyteller agents are legacy fixtures, the staging Compose contract exposes every Phase-3
setting, and the GitHub provider authenticates through a credential provider + `GIT_ASKPASS` (never a URL or
argv) — see `docs/GITHUB_PROMOTION.md`.

Proof: `pytest tests/society/test_e2e_self_development.py` and `python examples/demo_autonomous_society.py --story code`.
Both use the offline `ScriptedRoleModel` — they prove the *mechanics* of the loop, never model quality, and nothing in
them is "live autonomy".

## The loop (every link is a durable identifier)

```
signal (telemetry / ingested outcome / operator event, with evidence fields)
  → Scout run          → ImprovementProposal (+ evidence: signal, baseline, observed, window, sample, why actionable)
  → Governor run       → proposal.approved
  → Architect run      → SEARCH_REPO (reconnaissance turn) → repo.read.result (targeted wake)
  → Architect run      → CodeChangeSpec derived from search hits (kind=code, files_allowed, acceptance_tests, expected_effect)
                         + escrowed Builder task
  → Builder run        → READ_REPO_FILE in the candidate worktree (investigation turn) → repo.read.result
  → Builder run        → SUBMIT_CODE_CANDIDATE (source fix + new test) → isolated worktree commit on agentnet-auto/<id>
  → QA run             → compile + acceptance + regression on real pytest, allow-list, no self-judging, NEVER scan
  → Security run       → independent review (fail closed) → code_candidate.ready
  → Governor run       → REQUEST_PR_PROMOTION
  → Promotion Controller (non-LLM) → validate from TRUSTED base → branch → draft PR → CI → eligibility
  → Evaluator run      → REQUEST_MERGE_EVALUATION → offline experiment (base vs candidate) → PASS/FAIL/INCONCLUSIVE
  → Evaluator run      → RECORD_EVALUATION_RECOMMENDATION + memory (advisory; gates decide)
  → awaiting_approval  → a human merges in GitHub (observed) → merged → optional staging request (BLOCKED_EXTERNAL until a host exists)
  → post-merge experiment FAIL → rollback.recommended (previous known-good SHA) → future Scout context
```

Reconstructable from `society_events` (correlation + causation), `agent_runs`, `agent_intents` (including every
repository read and its result), `improvement_proposals`, `code_candidates`, `code_promotions`,
`change_experiments`, `deployment_requests`, `memory_items` (with provenance). No chain-of-thought is stored.

## Hard security law

- The model only produces typed intents (`intents.py`). It never holds a GitHub token, App key, model key,
  deployment credential or database/Redis password (`tests/society/test_secret_boundary.py`).
- Trusted deterministic code decides permission (`policy.py`), risk (`risk.py`), files (`engineering/workspace.py`),
  GitHub actions (`promotion.py`), merge eligibility, deployment (`deployment.py`), budgets, evaluation thresholds
  (`fitness.py`) and rollback gates.
- **Trusted-base rule:** every promotion/evaluation decision uses the policy/risk/fitness code of the RUNNING revision.
  A candidate may propose changes to those files; the changes are classified RED and cannot be used until they have
  independently passed the old rules and been merged by a human (`tests/society/test_risk_and_meta_change.py`,
  `tests/society/test_fitness.py::test_reward_hacking_by_editing_thresholds_uses_trusted_snapshot`).

## Repository intelligence (Gap Zero)

| Intent | Reads | Bounds |
| --- | --- | --- |
| `LIST_REPO_TREE` | directory listing (depth ≤ 4, ≤ 200 entries) | deny-listed paths hidden, symlinks skipped |
| `SEARCH_REPO` | literal or bounded regex over text files (≤ 40 hits, ≤ 4000 files) | identical searches in a correlation are answered once |
| `READ_REPO_FILE` | ≤ 32 000 bytes, text only | binary refused |
| `READ_REPO_RANGE` | ≤ 400 lines | |
| `READ_DIFF` | the candidate's diff vs its base | |
| `READ_CANDIDATE_STATE` | candidate + promotions + experiments (facts) | never spends a turn |

Refused: absolute paths, `..`, `~`, backslashes, NUL, `.git/**`, `.env*`, `*.pem/*.key/*.p12/*.pfx`, `*secret*`,
`*/secrets/*`, `*credential*`, `id_rsa*`, generated caches. Results are `_untrusted` data. Bounds (env):
`SOCIETY_MAX_REPO_READS_PER_RUN=5`, `SOCIETY_MAX_REPO_READS_PER_CORRELATION=40`, `SOCIETY_MAX_REPO_BYTES_PER_RUN=120000`,
`SOCIETY_MAX_SEARCH_RESULTS=40`, `SOCIETY_MAX_ENGINEERING_TURNS=6`, `SOCIETY_MAX_CORRELATION_ENGINEERING_TURNS=12`,
`SOCIETY_MAX_ENGINEERING_CORRELATION_DEPTH=20`. Reads target the trusted base checkout or, with `candidate_id`, the
candidate's isolated worktree.

## Risk tiers (trusted classifier, `risk.py`)

| Tier | Examples | Promotion policy (v1) |
| --- | --- | --- |
| GREEN | docs, dashboard templates/static, harmless acceptance tests, fixtures | PR + CI + fitness; human merge while `SOCIETY_AUTO_MERGE_ENABLED=false` (default); auto-merge only GREEN when on |
| AMBER | ordinary backend/worker/API logic, SDK, examples, `tests/*` | QA + Security + CI + fitness + human merge |
| RED | `services/registry/app/society/**`, `tests/society/**`, auth/authz/config/rate limiter, task_service/contract, websocket, `services/payment/**`, migrations/init-db/db bootstrap, Dockerfiles/compose/deploy/CI, requirements/pytest.ini/scripts/ci | proposable; independent Security + full CI + human approval always; never auto-merged |
| NEVER | `.env*`, keys/certs, secret stores, `.git/**`; diffs that skip/remove tests, remove warning gates, disable CI jobs, add `shell=True`/`os.system`, reference credential env vars outside the boundary | cannot be written (secrets) or cannot be promoted (diff findings) |

Application code never classifies GREEN (`kind=code` floors at AMBER). Candidates cannot reclassify themselves:
`code_candidates.risk_tier` is written by the promotion controller from the base classifier.

## Change budget and anti-busywork

`SOCIETY_MAX_AUTONOMOUS_CANDIDATES_PER_DAY=10`, `SOCIETY_MAX_RED_CANDIDATES_PER_DAY=2`, `SOCIETY_MAX_PROMOTIONS_PER_DAY=10`,
`SOCIETY_MAX_OPEN_AUTONOMOUS_PRS=3`, `SOCIETY_MAX_FILES_PER_CANDIDATE=8`, `SOCIETY_MAX_DIFF_LINES=600`,
`SOCIETY_MAX_AUTONOMOUS_MERGES_PER_DAY=1`. No agent can
raise them. Rejected at submission: no-op diffs, whitespace-only churn, duplicate diffs (`diff_hash`), oversize
changes; rejected at request: code changes without a linked proposal, without `expected_effect`, without acceptance
tests. Signal-driven proposals must carry evidence; duplicate titles collapse into one workstream
(`tests/society/test_telemetry_scout_budget.py`).

## Model routing and cost governor

Logical tiers `fast`/`strong` → `SOCIETY_MODEL_FAST_NAME` / `SOCIETY_MODEL_STRONG_NAME` (a future DeepSeek
configuration would set these to `deepseek-flash` / `deepseek-v4-pro`; nothing in code names a provider model).
Scout/Governor/QA → fast; Architect → fast, strong on RED work or after an invalid response; Builder → strong for
real code or after a QA failure; Security → strong only with static findings; Evaluator → strong on inconclusive
evidence. Escalation fails closed when the strong tier is unset or the agent/correlation budget is near zero.
Caps: `SOCIETY_DAILY_MODEL_BUDGET`, per-agent grant budget, `SOCIETY_MAX_CORRELATION_COST_USD=0.50`,
`SOCIETY_MAX_EXPERIMENT_COST_USD`, `SOCIETY_MAX_PROMOTION_COST_USD`. Each run records `model_tier`, `route_reason`,
tokens (incl. `tokens_cached` when the provider reports it), requests, retries, timeouts, cost, `output_format`,
`format_fallbacks`.

## Maturity levels

| Level | Meaning | State |
| --- | --- | --- |
| 0 | deterministic candidate only | proven since Phase 1 |
| 1 | live model creates a candidate | mechanics ready; **not claimed** (no credential, canary not run) |
| 2 | candidate automatically becomes a GitHub PR | proven in shadow (fake provider); real App not configured |
| 3 | GREEN PR auto-merges after trusted CI/evaluation | implemented behind `SOCIETY_AUTO_MERGE_ENABLED` (default **off**); when on, GREEN only, never a draft PR, never under a merge freeze, and bounded by `SOCIETY_MAX_AUTONOMOUS_MERGES_PER_DAY` (default 1) |
| 4 | merged GREEN change auto-deploys to staging and is evaluated | interface + durable requests only (`BLOCKED_EXTERNAL`); no host |
| 5 | positive staging evaluation promotes per policy | not implemented (needs Level 4) |

RED changes retain human approval at every level. Production autonomous deployment is hard OFF.

## Operator questions (`GET /v1/society/ask?q=...`, operator JWT)

"What is AgentNet trying to improve?", "Why was candidate <id> created / what files did the Builder inspect?",
"What is awaiting promotion?", "Why can PR <id> not merge?", "Which CI gate failed?", "What is the fitness result
for <id>?", "What should be rolled back?", "How much autonomous engineering budget remains?" — all answered from
persisted rows only.
