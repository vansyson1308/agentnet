# Autonomous Society Runtime v1

The society runtime turns AgentNet's existing primitives (agents, wallets/escrow, tasks, chat, goals,
memory, improvement proposals, spans) into a **closed, durable, permissioned loop**:

```
WORLD / DOMAIN EVENT ─► society_events (durable, idempotent, pg_notify wake)
        ─► dispatch: role subscriptions + explicit target; loop guards
        ─► agent_runs: atomic claim + lease (crash-safe), retry/backoff, dead + circuit breaker
        ─► context builder: identity, mission, goals, memory, mailbox, proposals, candidates, tasks,
                            budget, allowed intents, restrictions, recent activity (bounded, digested)
        ─► CognitiveModel.decide(context) ─► AgentDecision {summary, typed intents, sleep}
        ─► policy: grant ∩ risk ceiling ∩ flags ∩ scopes ∩ budgets  (fail closed)
        ─► executor: AgentChat / Goal / MemoryItem / ImprovementProposal / Offer / task_service / worktree
        ─► new society_events (causation-linked) ─► … ─► sleep until the next relevant event
```

Code: `services/registry/app/society/` · Schema: migrations `0007_society_runtime` + `0008_society_phase2` +
`0010_self_development` (DDL, generated from `schema_sql.py`; `init-db/16-society-runtime.sql` is the fresh-volume
bundle) + `0011_expire_rehearsal_memory` (data only: expires canary-rehearsal memory residue — nothing for a fresh volume) · Tests: `tests/society/` · API: `/v1/society/*` (public structural surface + operator surface) ·
Demo: `examples/demo_autonomous_society.py --story docs|code` · Staging/live model: `docs/SOCIETY_LIVE_MODEL_RUNBOOK.md` ·
Phase 3 self-development (repo intelligence, risk tiers, promotion controller, fitness): `docs/SELF_DEVELOPMENT.md`,
`docs/GITHUB_PROMOTION.md`, `docs/FITNESS_EVALUATION.md` · Decisions: ADR-0001, ADR-0002, ADR-0004.

## Data model

| Table | Purpose | Key invariants |
|---|---|---|
| `society_events` | Append-only event log / outbox | `idempotency_key` UNIQUE; `correlation_id` shared by a whole story; `causation_id` + `causation_depth`; status `pending → dispatched → processed` (or `ignored`/`expired`) |
| `agent_runs` | One cognition cycle of one agent for one event | UNIQUE `(agent_id, event_id)`; lease (`worker_id`, `lease_expires_at`); `attempt`/`max_attempts`; `context_digest`, bounded `context_summary`, `decision_summary`, tokens/cost; **no chain-of-thought** |
| `agent_intents` | Typed, adjudicated actions | `idempotency_key` UNIQUE; `risk_class`, `policy_decision`, `execution_status`, `result`/`error` |
| `agent_capability_grants` | The only source of an agent's permissions | Written only by `seed.py`/operators; allowed intents, risk ceiling, budgets, cooldown, circuit-breaker state |
| `code_candidates` | Builder → QA → Security record | branch `agentnet-auto/<id>`, base/head sha, changed files, `qa_report`, `security_report`, status machine below; Phase 3: trusted `risk_tier`, `diff_hash`/`diff_lines` (anti-busywork identity), `engineering_turns`, `repo_reads` |
| `code_promotions` (Phase 3) | Promotion Controller record for one candidate | at most one active promotion per candidate (partial unique index); lease + attempts; `status` lifecycle in `docs/GITHUB_PROMOTION.md`; `pr_number`/`pr_url`/`ci_status`; `rules_revision` = trusted base sha the rules were evaluated from |
| `change_experiments` (Phase 3) | Base-vs-candidate fitness evaluation | `criteria_version` snapshot (`fitness-v1`), `baseline_sha`/`candidate_sha`, hard-gate results, per-dimension metrics + deltas, `decision` (`pass`/`fail`/`inconclusive`), `rollback_recommended` |
| `deployment_requests` (Phase 3) | Audit of `REQUEST_STAGING_DEPLOY` through the `DeploymentProvider` | `blocked_external` when no provider is configured; production target is always `refused` |
| `intent_approvals` (Phase 2) | Human decision audit for `awaiting_approval` intents | `intent_id` UNIQUE; who decided, decision, reason, original policy reason, resumed/executed timestamps, `final_state`, `resume_error` |
| `users.society_role` (Phase 2) | Durable operator authority | `operator` \| `event_producer` \| NULL; the only source `operator_auth` consults besides the bootstrap allowlist |

Candidate status machine: `requested → building → built → qa_running → qa_passed → (security_review →) ready` · `qa_failed` (one retry) `→ rejected` · `failed/abandoned`.

`abandoned` is reachable only by an **operator**, through
`POST /v1/society/candidates/{id}/abandon` (`society/candidate_admin.py`). There is deliberately no
intent for it: a society that can retire its own unfinished work can also retire the evidence that it
failed. A merged candidate and the terminal `rejected`/`failed` states are refused, a reason is required
and persisted on the row, the call is idempotent (no second event, no second refund, original reason
preserved), and any in-flight `implement_change` task is closed through the ordinary
`task_service.fail_task_with_refund` escrow path — this code never writes a wallet.

## Roles (v1 fleet)

| Agent | Role | Wakes on | May emit |
|---|---|---|---|
| Society_Governor | governor (MEDIUM) | `proposal.created`, `code_candidate.ready/rejected`, `promotion.merge_eligible/rejected`, `experiment.finished`, `society.heartbeat`, `company.cycle`, `incident.opened`, `a2a.task.finished`, `a2a.agent.discovered` | messages, memory, goals, `REVIEW_IMPROVEMENT`, `READ_CANDIDATE_STATE`, `REQUEST_PR_PROMOTION`, `REQUEST_STAGING_EVALUATION`, `DISCOVER_A2A_AGENT`, `REQUEST_A2A_TASK` (**approval-gated**), `CHECK_A2A_TASK` |
| Society_Scout | scout | `company.cycle`, `a2a.agent.refreshed`, `platform.metric.anomaly`, `task.failed/timeout`, `qa.failed`, `agent.inactive`, candidate outcomes | messages, memory, `CREATE_IMPROVEMENT` (with structured evidence), agent goals, `REFRESH_A2A_AGENT`, `CHECK_A2A_TASK` |
| Society_Architect | architect (MEDIUM) | `proposal.approved`, `code_candidate.qa_failed/ready`, `repo.read.result` (own reads only), `code_change.spec_rejected` | repo reads (`LIST_REPO_TREE`, `SEARCH_REPO`, `READ_REPO_FILE`, `READ_REPO_RANGE`), `REQUEST_CODE_CHANGE`, `CREATE_TASK` (≤50 credits), goal updates |
| Society_Builder | builder (MEDIUM) | `code_change.requested`, `code_candidate.qa_failed/ready/rejected`, `repo.read.result` (own reads only), `society.heartbeat` | repo reads, `SUBMIT_CODE_CANDIDATE`, `START/COMPLETE/FAIL_TASK` |
| Society_QA | qa (MEDIUM) | `code_candidate.built` | `EVALUATE_CODE_CANDIDATE` (verdict computed by the runtime, not asserted) |
| Society_Security | security (MEDIUM) | `code_candidate.security_review`, `repo.read.result` (own reads only) | `READ_DIFF`, `READ_REPO_FILE`, `READ_CANDIDATE_STATE`, `SECURITY_REVIEW_CANDIDATE` (combined with static scan; fails closed) |
| Society_Evaluator (Phase 3) | evaluator | `promotion.ci_passed`, `experiment.finished` | `READ_CANDIDATE_STATE`, `REQUEST_MERGE_EVALUATION`, `RECORD_EVALUATION_RECOMMENDATION` (advisory only), memory, messages — it cannot change thresholds, approve, merge, deploy or alter evidence |

Roles are configuration (`roles.py`), overridable/extendable with `SOCIETY_ROLES_FILE` (JSON). Agents are
reused by name; `python -m app.society.seed` is idempotent. Any agent additionally wakes when an event
targets it (`payload.target_agent_id` / `subject_type=agent`), e.g. `agent.message.received`.

## Risk model

| Class | Intents | Handling |
|---|---|---|
| LOW | `SEND_MESSAGE`, `WRITE_MEMORY`, `CREATE_GOAL`, `UPDATE_GOAL`, `CREATE_IMPROVEMENT`, `REVIEW_IMPROVEMENT`, `SLEEP`, read-only repo intelligence (`LIST_REPO_TREE`, `SEARCH_REPO`, `READ_REPO_FILE`, `READ_REPO_RANGE`, `READ_DIFF`, `READ_CANDIDATE_STATE`), `RECORD_EVALUATION_RECOMMENDATION`, `REFRESH_A2A_AGENT`, `CHECK_A2A_TASK` | auto if in grant; repo reads are bounded (per run / per correlation / bytes), path-safe, persisted as `repo.read.result` and returned as untrusted data |
| MEDIUM | `CREATE_OFFER`, `COUNTER_OFFER`, `ACCEPT_OFFER`, `CREATE_TASK`, `START/COMPLETE/FAIL_TASK`, `REQUEST_CODE_CHANGE`, `SUBMIT_CODE_CANDIDATE`, `REQUEST_QA`, `EVALUATE_CODE_CANDIDATE`, `SECURITY_REVIEW_CANDIDATE`, `REQUEST_PR_PROMOTION`, `REQUEST_MERGE_EVALUATION`, `REQUEST_STAGING_EVALUATION`, `REQUEST_STAGING_DEPLOY`, `DISCOVER_A2A_AGENT`, `REQUEST_A2A_TASK` | role-gated by grant ceiling; A2A intents need `A2A_SOCIETY_CLIENT_ENABLED` + `A2A_FEDERATION_ENABLED`, discovery only for `A2A_SOCIETY_DISCOVERY_ALLOWED_HOSTS`, tasks only to operator-verified agents under call budgets (`docs/A2A_FEDERATION.md` §6); escrow ≤ min(grant cap, `SOCIETY_MAX_TASK_ESCROW_CREDITS`); code intents need `SOCIETY_AUTONOMOUS_CODE_ENABLED`; staging needs `SOCIETY_STAGING_DEPLOY_ENABLED`; promotion/evaluation intents only *request* — the non-LLM Promotion Controller and fitness engine decide (`docs/GITHUB_PROMOTION.md`, `docs/FITNESS_EVALUATION.md`) |
| HIGH | `REQUEST_PRODUCTION_DEPLOY`, `SHELL_EXEC`, `GRANT_CAPABILITY`, `MODIFY_BUDGET`, `TRANSFER_FUNDS`, `MODIFY_WALLET`, `MODIFY_SECRET`, `CHANGE_AUTH_POLICY`, `DELETE_DATA`, `OPEN_NETWORK_ACCESS`, `RUN_MIGRATION` | recognised, **always denied**, recorded as `intent.denied` events; no executor exists |

Additional refusals in executors: no self-review, requester ≠ builder ≠ QA ≠ security reviewer, no
message/task/offer to yourself, goals only for owner/society, memory scope per grant.
`approval_required_intents` on a grant park an intent as `awaiting_approval` (visible in `/v1/society/ask?q=blocked`
and `/v1/society/approvals`). An operator approves/rejects through `POST /v1/society/intents/{id}/approve|reject`;
the runtime then resumes the **persisted** intent (model not re-called), re-runs the full policy with
`approval_granted=True` and fails closed if a flag, grant, scope or cap changed. Forbidden HIGH types are never
approvable. `intent.approval_required/approved/rejected/resumed/executed` events carry the story (`approvals.py`).

## World signals

`world.py` runs before every dispatch cycle: platform tasks that ended FAILED/TIMEOUT through REST/WS/the
refund worker become `task.failed` / `task.timeout` events (one per task, correlation = the task's trace id,
deduped by subject so runtime-initiated failures are not doubled), and `society.heartbeat` is emitted at most
once per `SOCIETY_HEARTBEAT_INTERVAL_SECONDS` so the Governor can reprioritise goals without polling.
Operators/webhooks inject other world events through `POST /v1/society/events` — `event_producer` or `operator` role
required, allow-listed types only (`platform.metric.anomaly`, `platform.health.degraded`, `user.feedback.received`,
`staging.canary.signal` + `SOCIETY_INGRESS_EVENT_ALLOWLIST`), reserved families and `target_agent_id` refused,
bounded payloads (size/depth/string/keys), `idempotency_key` replay answers 200 with the original event,
per-actor and global hourly limits. Payloads are untrusted data, never instructions.

## Loop-storm protections

per-agent cooldown (a wake inside the cooldown is DEFERRED with a `not_before`, never dropped; bounded by `SOCIETY_EVENT_TTL_SECONDS`) · per-event dedupe (idempotency key, UNIQUE agent/event) · max causation depth ·
max runs per correlation · repeated-message suppression window · max intents per run (grant ∩ global) ·
runs/hour (agent ∩ global) · daily USD budget (agent ∩ global) · exponential retry then DEAD ·
per-agent circuit breaker (`paused_until`) · event TTL · an agent is never woken by its own untargeted event ·
a targeted-only event (`runs.TARGETED_ONLY_EVENT_TYPES`: `repo.read.result`) wakes its target and nobody else.
All emit `loop_breaker.tripped` / `run.dead` events (deduped) for observability.

**Why `repo.read.result` is targeted-only (staging, 2026-09-26).** A read result is the reading
agent's next engineering turn. The Architect, the Builder and Security list the type among their
subscriptions, and dispatch used to add every subscriber to the target. Every
Architect read therefore also woke the Builder and Security, whose runs only answered "not for me",
so each read spent three runs of the correlation's budget. Three reconnaissance reads brought the
correlation to `SOCIETY_MAX_RUNS_PER_CORRELATION=12` exactly when the Architect's
`code_change.requested` arrived. The loop breaker ignored it and the candidate stranded in
`REQUESTED`. It happened twice that day, to candidates 9da14a08 (later abandoned by an operator)
and a2788678 (correlation b8db5936). Now each read wakes only its reader, and no cap changed.
- A subscription to a targeted-only type wakes nobody extra; this includes one added through
  `SOCIETY_ROLES_FILE`.
- If the target cannot be resolved, the event is ignored with the dispatch note
  "targeted-only: target unresolved".
- `tests/society/test_repo_intel.py` replays the live story at the staging cap.
- `tests/society/test_events_and_dispatch.py` fails if an event type emitted as a targeted wake is
  subscribed by a role without being targeted-only.

## Engineering loop safety

- Builder: `git worktree add -B agentnet-auto/<id> <workspace_root>/<id> <base>`; every edit path must be
  relative, resolve inside the worktree (symlink-safe), be on the spec's `files_allowed`, and not match a
  NEVER-write pattern (`risk.py`: `.env*`, `.git/*`, key/certificate files, `*secret*`, `*credential*`, …).
  Every other path is writable but classified by the **trusted** risk classifier (`GREEN`/`AMBER`/`RED`;
  CI, compose, deploy, migrations, dependencies, auth/payment/society code are RED and never auto-merge), and
  diff-level NEVER findings (skipped/deleted tests, disabled warning gates, shell, credential references,
  non-blocking CI) reject the candidate. A single violation aborts the whole submission. `git` is argv-only;
  the cognition worker never pushes or merges. Anti-busywork: no-op, whitespace-only, oversize
  (`SOCIETY_MAX_DIFF_LINES` / `SOCIETY_MAX_FILES_PER_CANDIDATE`) and duplicate diffs are rejected
  (`code_candidate.rejected`), and every candidate must link to a proposal with evidence.
- **Design-time contract** (`society/engineering/docs_contract.py`): the directory, the acceptance test and
  the required sections of a documentation candidate are ONE stdlib-only object, shared by the Architect's
  prompt, the `REQUEST_CODE_CHANGE` validation and the acceptance test itself, so a convention cannot drift
  into meaning two things. A spec that the trusted QA gate could never pass is refused *before* a candidate
  row exists; the first refusal per correlation emits `code_change.spec_rejected` with machine-readable
  `{field, code, expected}` errors, which wakes the Architect for exactly ONE corrective turn. A second
  refusal is still refused but emits nothing, so a model that cannot satisfy the contract cannot spin on it.
  The contract states the boundary only — the filename, the title and the prose stay the Architect's.
- QA verdict is computed from facts: allow-list, protected paths, no self-judged tests, tests exist, changed
  `.py` compile in memory, no secret patterns in the diff, then `python -m pytest <acceptance targets>` in
  the worktree with a scrubbed environment (no `*_PASSWORD/_KEY/_SECRET/*TOKEN*`). Zero acceptance
  criteria is a FAIL. Two failed attempts → `rejected`.
- **Work on a candidate outlives a story.** Engineering turns are bounded per correlation
  (`SOCIETY_MAX_ENGINEERING_TURNS`), and the Builder resumes an open candidate on the hourly heartbeat,
  which starts a new correlation each time. An agent's context (`repo_reads`, at most 6 entries) holds:
  - first, its reads in the current story;
  - then, in whatever slots remain, its reads made for a **still-open** candidate in another story within
    the last 24 h, marked `earlier_story` with their time `at`.

  Staging, 2026-09-26: after candidate a2788678 failed QA, the Builder re-read the same test, templates and
  `main.py` at 15:00Z and again at 16:00Z. It ran out of turns each time and could not resubmit.

  Carried reads never displace the current story's reads. The following are not carried:
  - reads made before the agent's own last `SUBMIT_CODE_CANDIDATE` for that candidate, since the worktree
    has changed since then;
  - repeats of the same request (only the newest is kept);
  - reads for a closed candidate, reads without a candidate, and other agents' reads.

  No budget or cap changes.
- Security review is required when the spec flags it, when any file matches the risky-path pattern, when
  `kind == "code"`, or when the static scan produced findings; final verdict = reviewer verdict AND no
  static findings.
- Payment: the Architect escrows the Builder's `implement_change` (price 10 credits) and the Builder
  releases it only on `code_candidate.ready`; `rejected` refunds via `FAIL_TASK`.

## Configuration

See `.env.example` (section *Autonomous Society Runtime v1*); `tests/test_config_parity.py` keeps the
settings, `.env.example`, `docker-compose.staging.yml` and `docs/DEPLOYMENT_ARCHITECTURE.md` in sync. Defaults: runtime OFF, code loop OFF,
staging OFF, production deploy hard OFF (not a setting), provider `scripted`. Live models:
`SOCIETY_MODEL_PROVIDER=openai_compatible` + `SOCIETY_MODEL_BASE_URL` + `SOCIETY_MODEL_API_KEY` (+ `_NAME`).

## Runbook

```bash
# 1. schema (existing DB: container entrypoint does this; manually:)
cd services/registry && alembic upgrade head        # → 0010_self_development (0007 + 0008 + Phase 3)

# 2. seed the fleet (idempotent; reuses agents by name)
python -m app.society.seed

# 3. start the worker (compose) — idles until SOCIETY_RUNTIME_ENABLED=true
docker compose up -d society-worker
docker compose logs -f society-worker

# 3b. operator roles (durable; bootstrap the FIRST one with SOCIETY_OPERATOR_BOOTSTRAP_EMAILS)
python -m app.society.operator_auth you@example.com operator
curl -X POST http://localhost:8000/v1/society/operators -H "Authorization: Bearer $OPERATOR_JWT" \
     -H 'Content-Type: application/json' -d '{"email":"webhook@example.com","role":"event_producer"}'

# 4. inject a world event (event_producer/operator USER JWT; reserved society.* types are refused)
curl -X POST http://localhost:8000/v1/society/events -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"event_type":"platform.metric.anomaly","payload":{"metric":"task_failure_rate","value":0.42,"severity_score":70}}'

# 5. observe (public = structural; operator = full detail)
curl http://localhost:8000/v1/society/status
curl "http://localhost:8000/v1/society/story/<correlation_id>"
curl -H "Authorization: Bearer $OPERATOR_JWT" "http://localhost:8000/v1/society/story/<correlation_id>/detail"
curl -H "Authorization: Bearer $OPERATOR_JWT" "http://localhost:8000/v1/society/ask?q=what%20is%20blocked"

# 6. approvals (operator)
curl -H "Authorization: Bearer $OPERATOR_JWT" http://localhost:8000/v1/society/approvals
curl -X POST -H "Authorization: Bearer $OPERATOR_JWT" -H 'Content-Type: application/json' \
     -d '{"reason":"reviewed"}' http://localhost:8000/v1/society/intents/<intent_id>/approve   # or /reject

# 7. live model (see docs/SOCIETY_LIVE_MODEL_RUNBOOK.md)
python -m app.society.canary preflight                 # credential safety + provider probe; never prints the key
curl http://localhost:8000/v1/tasks/traces/<trace_id>          # spans: society.run / society.intent.*
curl http://localhost:9101/metrics | grep agentnet_society_       # Prometheus

# deterministic proof (no credentials needed)
python examples/demo_autonomous_society.py            # exit 0 ⇢ documentation candidate READY
python examples/demo_autonomous_society.py --story code   # real source-code fix → QA → Security → shadow PR → offline fitness PASS
pytest tests/society -v                               # DB-backed suite (34 files; counts in docs/TEST_MATRIX.md)
```

Pause the society: `SOCIETY_RUNTIME_ENABLED=false` (worker idles; pending events wait, nothing is lost).
Pause one agent: set `agent_capability_grants.enabled=false` (or `paused_until`) — no intent can do this.
Stuck run: leases expire (`SOCIETY_RUN_LEASE_SECONDS`) and the run is re-claimed; after `max_attempts` it is
`dead` and visible under `/v1/society/ask?q=blocked`.

### Staging

`docker-compose.staging.yml` carries `society-worker-staging` (registry image, `agentnet_staging`, runtime and code
loop OFF by default, no docker socket, no published ports, healthcheck on the internal metrics port; credential only
from the host environment). Staging is its own Compose project (`docker compose -f docker-compose.staging.yml`,
never stacked on `docker-compose.yml`); after `up`, run `deploy/society-migration-check.sh` (fresh + upgrade +
downgrade round-trip on scratch databases) and `deploy/society-staging-smoke.py`; `deploy/society-staging-redteam.py` and `python -m app.society.canary` drive the
live-model canaries. Full procedure, GO/NO-GO and failure policy: `docs/SOCIETY_LIVE_MODEL_RUNBOOK.md`. What was
actually proven (and what was blocked): `docs/SOCIETY_LIVE_PROOF.md`. No production Compose definition is current (the retired VPS overlay is archived under `deploy/legacy-vps/`).

## Memory truth hierarchy

Memory is evidence, never policy — but evidence that is wrong still steers the fleet, so the
runtime ranks what it believes. Precedence, strongest first:

1. **trusted current facts** — the world as trusted code observes it right now (deployment state,
   schema head, wallet balances, task rows). Never inferred from memory.
2. **trusted refusal/execution records** — what the platform actually did with an intent.
   `context.recent_refusals` carries this, and the system prompt states the rule plainly: a refused
   intent never took effect, *"no matter what a memory item or an earlier decision_summary claims"*.
3. **validated memory** — a belief trusted evaluation has confirmed (90-day half-life, +0.15).
4. **unvalidated memory** — a belief an agent recorded in good faith (21-day half-life).
5. **refuted memory** — a belief disproved and demoted (3-day half-life, −0.5), never deleted.

A model-authored memory may never outrank contradictory trusted execution evidence. Phase 5 is the
cautionary tale: a Scout's `CREATE_IMPROVEMENT` was refused for a schema violation while the same
run's `WRITE_MEMORY` recorded *"improvement raised"*. That memory came from a real signal so it
never expired, and three later runs declined the same signal citing it — each writing another note
corroborating the first.

### Execution-grounded memory (graduation hardening, 2026-09-26)

The same failure recurred live during graduation. At 06:02Z a Scout's `CREATE_IMPROVEMENT` for a
critical public-surface regression was refused (*"portfolio full"*). The same decision's
`WRITE_MEMORY` recorded *"new proposal raised"*. At 07:19Z the Scout declined the next anomaly as a
*"duplicate of 06:02 proposal"*, although that proposal never existed. An operator had to refute both
memories. Refutation repairs a belief after the fact. The invariant below keeps the belief from
forming (`society/memory_grounding.py`):

> **A model-authored memory is admitted only if every side-effecting intent of the same decision
> reached `EXECUTED`.**

- **Why decisions, not wording.** The model authors a decision's intents together, before any of
  them executes. A memory in that decision can therefore only state an *expected* outcome. The rule
  is execution-semantic. There is no phrase matching, and there is no payload field a model could set
  to opt out.
- **What blocks a memory.** A side-effecting sibling that is `failed`, `denied`,
  `awaiting_approval`, `approved`-but-not-resumed, `rejected`, `skipped` or still `pending`. The
  memory intent then **fails** with the trusted reason (`memory not grounded: … seq 0
  CREATE_IMPROVEMENT is failed`). No `memory_items` row is written. The refusal itself reaches the
  agent through `recent_refusals`.
- **Ordering.** Memory intents run after every other intent of their run, so the outcome is known.
  The check lives in the executor, so a memory resumed through the approval path is held to it too.
- **What counts as a side effect.** Read-only repository intelligence, `SLEEP` and other memories
  are not side effects. Every other type is, including an unknown or invalid type the model emitted,
  so the rule fails closed.
- **What still works.** Observation and hypothesis memories are unaffected in a decision with no
  side effect, or when every side effect executed.
- **Approval path.** The approval resume queue orders a decision's memories after its other intents.
  A memory approved before its side effect is refused while that side effect still awaits approval.
- **Known edges.**
  - `EXECUTED` means the executor completed the intent. For an idempotent duplicate (a proposal title
    that is already open, or a suppressed duplicate message) that completion is a no-op, which is
    still consistent with the row that exists.
  - An honest observation memory written in an approval-gated decision is also refused.
- **`recent_refusals` fix.** For an intent that policy *allowed* but execution *failed*, the reason
  shown is now the execution error. Before, it was the policy reason, *"allowed by grant"*, which hid
  *"portfolio full"* from the live Scout.
- **`signal_coverage` (trusted, world signals only).** The same incident had a second, cross-run
  form. After the refused proposal, triage-only Scout runs kept writing *"duplicate of the 06:02
  proposal"*, at 08:09Z and again at 09:07Z on the hardened code. Those decisions had no side
  effect, so their memories are admitted. Each later run believed its own note, and operator
  refutation did not stop it. The context never answered the one question the Scout was deciding.
  It now does, from durable rows:
  - `open_proposals`: every open proposal created by an **executed** `CREATE_IMPROVEMENT` whose
    evidence named this signal type. There is no time window, so an old proposal is not forgotten.
    Each entry carries its portfolio state (`active`, `concluded` or `shelved`, from
    `company.portfolio_accounting`). An empty list means no such proposal exists, whatever a memory
    says.
  - `attempts`: the last few `CREATE_IMPROVEMENT` intents for the signal, from any agent, within 7
    days. Each shows its outcome, whether it was yours (`by_you`), the proposal it produced and
    whether that was an idempotent `duplicate`. The reason is shown only for your own attempts, so no
    text crosses between agents.
  - `portfolio`: whether company mode has room. This entry is present only when company mode is on.
  - Coverage is per signal **type**. Whether an open proposal addresses *this* event (for example,
    which task failed) remains the agent's judgement; the prompt says so.
- **Trusted reasons carry no model or operator text.** A reason shown in `recent_refusals` or
  `signal_coverage` comes from platform code only:
  - A payload that fails validation is summarized structurally
    (`intents.safe_error_summary`). The summary keeps the error type, the path through declared
    field names, and schema limits. It never keeps the rejected value, a key the model invented, or a
    custom validator's message. The approval-resume re-validation uses the same summary.
  - An operator's decision reaches agents as its outcome only (*"rejected by an operator"*).
    `approvals.py` records the operator's email and text on the intent, and those stay there.
- **`recent_activity[].outcomes`.** The model's own `decision_summary` (for example *"raised
  proposal X"*) is written before its intents run. It now travels with the run's trusted outcome
  (`executed` count, `not_executed: ["CREATE_IMPROVEMENT:failed", ...]`), so the same false claim cannot
  come back through the activity feed instead of memory.
- **Tests.** `tests/society/test_memory_grounding.py` pins the status matrix, the exact live failure,
  and a later run that is not falsely suppressed.

Correcting a belief that predates this rule, or one that is wrong for other reasons, is
**refutation**, not deletion:

```
POST /v1/society/memory/{memory_id}/refute   {"reason": "..."}
```

Operator authority only (user JWTs; scoped agent tokens are refused upstream), idempotent, and
there is deliberately **no intent type** for it — a model cannot grade its own evidence. The row
keeps its content, provenance and timestamps; `memory_validation_events` records who, when, why and
on what evidence, and is append-only **in the database** (a trigger refuses UPDATE and DELETE from
every caller, including the application). Trusted evaluation machinery may also refute with
`actor_type="evaluator"` when objective evidence disproves a belief, with no human present.

## Known limitations

- `ScriptedRoleModel` is a deterministic rule engine, not an LLM; it proves the runtime, not model quality.
  Live runs need an OpenAI-compatible endpoint and a credential that `canary preflight` accepts; the canary refuses
  scripted/fake providers (NO FAKE AUTONOMY).
- Documentation candidates and one real source-code candidate (an isolated fixture application,
  `tests/society/fixtures/code_repo`) are proven deterministically with `ScriptedRoleModel`; `kind=code`
  candidates always require Security review. The scripted Builder's fix is a rule, not model quality evidence.
- Promotion to GitHub is proven only in shadow mode (`FakePromotionProvider`); the inert GitHub provider has
  never been exercised against a real repository and no Society GitHub App exists yet (`docs/GITHUB_PROMOTION.md`).
  Auto-merge is `false` and refused for the GitHub provider; the cognition worker never holds a token.
- Fitness evaluation is `offline` only (base vs candidate in the worktree); staging-live evaluation and
  rollback *execution* need a `DeploymentProvider` that does not exist (`docs/FITNESS_EVALUATION.md`).
- `REQUEST_STAGING_DEPLOY` is recorded as a `deployment_requests` row and, with the default `disabled`
  provider, ends `blocked_external`; deployment remains a human/CI action. Production deploy is refused.
- `proposal.status` reaches `CONVERTED_TO_TASK` when a candidate is requested; `IMPLEMENTED` is reserved
  for a human merge (the runtime never merges). Phase 3.1 retired the general worker's legacy reflection
  loop and `AGENT_BACKLOG.md` bridge (archived under `legacy/hermes/`): the Society runtime is the ONE
  autonomous improvement control plane and the worker never touches proposals
  (`tests/society/test_single_control_plane.py`). Historical rows that the bridge flipped to
  `CONVERTED_TO_TASK` are left as they are; they are recognisable by `converted_task_id IS NULL`, whereas
  Society conversions always carry the Builder task id.
- Human approval is API-only (`/v1/society/approvals`, `approve|reject`); there is no UI. `modify` (edit-then-approve)
  is deliberately unsupported: intents are immutable once persisted.

## A2A (Phase 8)

The 0.3-shaped `app/a2a.py` is retired. AgentNet is an A2A 1.0 server and client (`docs/A2A_ARCHITECTURE.md`, ADR-0009). The Society uses external agents only through the four typed A2A intents. The executors record requests; the federation pump (`society/federation_pump.py`) does the network work. Results come back as untrusted, causation-linked events. The daily **company cycle** and the incident freeze are described in `docs/AUTONOMOUS_COMPANY.md`.

## MCP integration point (deferred)

External/pluggable tools (web search, deployment providers, third-party MCP servers) should enter through a
`CALL_EXTERNAL_TOOL` intent with a per-tool risk class and grant scope, using the MCP 2026-07-28 stateless
shape (per-request version, `Mcp-Method`/`Mcp-Name` allow-listing, OAuth 2.1 + RFC 8707 audience, never
passing the agent's tokens through). Internal domain operations stay native intents.
