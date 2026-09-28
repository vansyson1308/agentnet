# Maintenance OS — live proof record

**Verdict: MAINTENANCE OS — BLOCKED (2026-09-28T02:50Z).** The Maintenance OS was merged dark in #65
(`main` 2386f8e) and its activation on Railway staging began on 2026-09-28. Monitoring is live on
staging and observes production. The first live cognition run found an implementation defect, and the
kernel handled it safely: bounded tries, escalation, nothing stranded. The fix, and the other defects
the live checks found, are in PR #66 (RED trusted base: the owner merges). The release proofs need
owner-only credentials. The table below and the activation record say exactly what is proven. The terminal
verdicts `AGENTNET AUTONOMOUS MAINTENANCE OS — LIVE` and `AUTONOMOUS PRODUCT-MAINTENANCE SYSTEM — PROVEN`
are **not** claimed and must not be claimed until every row below marked *required* has real
evidence recorded here.

## What is proven, and how (deterministic, CI)

| Claim | Evidence (all with scripted cognition and fake providers — mechanics, not model quality) |
|---|---|
| Observation → incident → case → diagnosis → immutable plan → PatchSet → deterministic QA → Review/Security → READY candidate → unchanged promotion controller → GREEN auto-merge → attested release → exact-SHA production PR → service-aware deploy → N healthy observations → incident recovery → `AUTO_REPAIRED` | `tests/society/maintenance/test_kernel_e2e.py::test_green_defect_is_repaired_released_and_verified_autonomously` |
| Controlled bad release → rollback to known-good → reconciliation PR + parity freeze → `AUTO_ROLLED_BACK` | `test_kernel_e2e.py::test_release_regression_rolls_back_to_known_good` |
| AMBER → complete package → `SAFELY_ESCALATED`, no agent runs while waiting, owner merge resumes the persisted case, release only after a verified owner merge | `test_kernel_scenarios.py::test_amber_*`, `test_owner_verified_amber_release_proceeds` |
| RED prepared, never released; constitutional scope `POLICY_REFUSED` | `test_red_repair_is_prepared_and_never_released`, `test_constitutional_scope_is_refused_before_any_build` |
| Process death before every transition converges without duplicate candidate/PR/release; a crashed activity is abandoned and retried | `test_crash_and_concurrency.py` |
| Two kernels / two release controllers / eight concurrent collectors → one authoritative history, one merge, one deploy, one incident | `test_crash_and_concurrency.py` |
| Model failures (invalid JSON, partial, timeout, rate limit, invalid output) retry by policy, then escalate; never stranded | `test_kernel_scenarios.py::test_model_failures_*` |
| GitHub/Railway failures bounded; conflicts, CI failure, moved production, unrelated changes, tampering, missing schema → refused; image expiry → known-good SHA redeploy; failed rollback → P0 freeze | `test_kernel_scenarios.py::test_github_*`, `test_release_controller_recomputes_and_refuses`, `test_rollback_falls_back_*` |
| 24 simulated hours with incidents, self-recoveries and flaky models: zero stranded cases at every 5-minute tick | `test_crash_and_concurrency.py::test_24_hours_*` |
| Healthy system → no repair, zero model calls | `test_kernel_scenarios.py::test_healthy_system_*` |
| Historical deadlocks of #51–#62 cannot occur on the maintenance path | `test_historical_replay.py` |
| Secret boundary, money untouched, watchdog, operator API, structural ingress | `test_boundaries.py` |
| The real product defects are detected deterministically (not from chat screenshots) | `deploy/maintenance/browser_probe.py --local-dashboard` findings in `MAINTENANCE_OBSERVABILITY.md`; `services/dashboard/tests/test_experience_contract.py` fails on `main` for exactly those reasons |

## Required live proofs — status

| # | Proof | Status | Evidence / what it needs |
|---|---|---|---|
| 1 | Migration 0014 on staging through the registry pre-deploy owner | **PROVEN** | registry deployment `f7e865ff` (main 2386f8e), log `Running upgrade 0013_a2a_federation -> 0014_maintenance_os` 01:39:19Z; observer S01–S05 PASS 01:49:55Z (head, 15 tables, liveness/terminal constraints, one-active-case / one-open-fingerprint / one-release-in-flight unique indexes, immutability triggers); fresh-install parity = CI job "Fresh install" green on 2386f8e |
| 2 | Monitoring-only on real production observations | **PROVEN** | 6 incidents opened 01:53:53Z from `public_surface_monitor` (see record); re-observed 02:03:59Z → `observation_count` 2, no new incident (dedup); 0 cases, 0 model calls, 0 patches, 0 releases while autonomy was off; `stranded=0` (W01 over 6 min) |
| 3 | Deep-tier browser findings reach the kernel (EXPERIENCE_QUALITY incidents) | **BLOCKED — owner secrets** | probe itself proven on GitHub's runner (run 36365366412, artifact 10947601107: contrast 1.06:1, raw dicts, error banner, dead links, wrong routes, axe serious); ingestion needs PR #66 (per-run login) + the ingest account and GitHub secrets (owner checkpoint C) |
| 4 | Live cognition (real DeepSeek) → DIAGNOSING → PLAN_READY → BUILDING → VERIFYING | **FAILED ONCE, FIX IN #66** | 02:14–02:21Z: 5 real cases reached DIAGNOSING on `openai_compatible`/`deepseek-flash`; every DiagnoseIncident try ended `turn_budget` (8 turns of reads, no answer) → `SAFELY_ESCALATED activity_exhausted:DiagnoseIncident`; no case reached PLAN_READY. Fix: activity loop reserves the final turn (#66, `test_activity_loop.py`) |
| 5 | Nothing stranded at every checkpoint | **PROVEN so far** | `reconciler.stranded_count=0` and the independent query = 0 at 01:49, 01:56, every minute 01:56–02:01 and 02:14–02:19, 02:08, 02:19, 02:30 |
| 6 | Kill switch | **PROVEN (first half)** | `MAINTENANCE_AUTONOMY_ENABLED=false` 02:21Z: the in-flight case `02d7ea59` was handed over `autonomy_disabled` (no cooldown), no further model call, monitoring kept observing (02:23:35Z observations), production UI/API/A2A answered 200 at 02:39Z; restore after #66 |
| 7 | Railway release API against the live schema | **PROVEN; adapter fixed in #66** | live introspection: `serviceInstanceDeployV2(commitSha, environmentId, serviceId) -> String!`, `deploymentRollback(id) -> Boolean!`; the old adapter form fails `GRAPHQL_VALIDATION_FAILED`, the new forms pass validation (unauthenticated probe, bogus id) |
| 8 | release-control dark boot | **PROVEN (from the #66 branch)** | service `release-control` (`ee1966b0`), staging, no domain, restart ALWAYS, no model/Society credential, provider `disabled`: `maintenance release controller starting (provider=disabled)` 02:08:04Z, heartbeat `release_controller` 47 cycles / 0 errors at 02:30Z. The first boot on main crash-looped (`setup_logging()` without a service name): fixed in #66 |
| 9 | Release preview (exact-SHA, non-production) | **IMPLEMENTED in #66, not yet live** | `LivePreview` + settings pre-set on release-control; needs a staging-scoped Railway token (owner checkpoint E) |
| 10 | Live release provider preflight, rollback on preview/staging, kernel restart mid-case, GREEN production repair, AMBER continuation | **NOT RUN** | need #66 merged, owner checkpoints B–E, then a real Society repair |
| 11 | Release-credential leak audit | **PARTIAL** | maintenance tables: no credential shape or credential env value (X01 PASS at every run); no release credential exists yet |

## Graduation invariant

At every live checkpoint record the operator status page's `nothing_stranded`
(`reconciler.stranded_count`) and the independent query (non-terminal, no active lease, no
`next_action_at`, not awaiting approval). Both have been 0 at every live checkpoint so far (row 5), measured
by `deploy/railway/maintenance_live.py` in the staging-validator.

## Why the external coding agent could not finish the live proofs

Claude built only the Maintenance OS infrastructure (mission items 78, 156–157). The live proofs need
(a) the owner to review and merge a constitutional (RED) change to the trusted base — the repository's
own law forbids the builder of the change from approving it; (b) credentials and infrastructure that
only the owner can create (a GitHub App registration, a production-scoped Railway token, a release
preview environment); and (c) a real Society repair authored by the Society, not by Claude.

## Live activation record — 2026-09-28 (staging observes production)

Repository: `main` 2386f8e (PR #65 merged), `production` 322e76b (unchanged). Production Society OFF
(`/v1/society/status` runtime_enabled=false), production API/UI/A2A card 200. Rulesets unchanged since
2026-09-19/21 (main, production; production has no bypass actor). PR #64 still a HELD draft.

Post-merge CI: run 36363312958 attempt 1 failed one test (`test_signal_coverage_contradicts_a_cross_run_duplicate_belief`)
at 00:54 UTC. The root cause is a time-of-day dependence in a Society test that PR #65 did not cause (see #66). Attempt 2 was
green at 01:37Z. Railway had skipped the Wait-for-CI deploys, so staging was deployed at the exact SHA:
registry `f7e865ff`, society-worker `515705f8`, dashboard `4bd791a6`.

| Time (UTC) | Event | IDs |
|---|---|---|
| 01:39:19 | migration 0013→0014 by the registry pre-deploy | deployment f7e865ff |
| 01:49:55 | observer: schema S01–S05 PASS, stranded 0, operator API 200 | validator 0ea0e013 |
| ~01:52 | society-worker + registry: `MAINTENANCE_MONITORING_ENABLED=true`, all other switches false | |
| 01:53:53 | 6 incidents from `public_surface_monitor` (target production) | 14bf577b marketplace UI_NAVIGATION P2 fp1:505bb6de…; c1d9cf36 login AUTH P1 fp1:9adccec1…; 1569e7f4 register AUTH P1 fp1:8fa6c5b7…; eaf7b1e2 landing:placeholders UI_NAVIGATION P2 fp1:1e96bc25…; 3e683e67 metaverse:placeholders UI_NAVIGATION P2 fp1:78c8c589…; 64171a56 network:placeholders UI_NAVIGATION P2 fp1:e1be9903… |
| 02:03:59 | second observation: observation_count 2, no new incident | |
| 02:08:04 | release-control dark boot (provider=disabled) | service ee1966b0, deployment be747180 (branch of #66, 8fe57e2) |
| ~02:13 | society-worker + registry: autonomy + cognition ON, GREEN promotion OFF | |
| 02:14:06 | 6 RepairCases opened, one per incident | 08db9765 (register), 9b47104f (login), e2f583dc (landing), 4a584219 (marketplace), 9fecc2a9 (network), 02d7ea59 (metaverse) |
| 02:14–02:21 | 5 cases DIAGNOSING → SAFELY_ESCALATED `activity_exhausted:DiagnoseIncident` (turn_budget ×3 each, real DeepSeek) + ExplainEscalation succeeded | 20 live activities, $0.037 total |
| 02:21 | kill switch `MAINTENANCE_AUTONOMY_ENABLED=false` | |
| 02:23:30 | 02d7ea59 handed over `autonomy_disabled` (no cooldown) | |
| 02:30:56 | observer: 6/6 SAFELY_ESCALATED, stranded 0, X01 PASS, kernel 426 cycles/0 errors | validator 2621771d |

The five `activity_exhausted` cases put their incidents in the 24 h reopen cooldown
(`MAINTENANCE_CASE_REOPEN_COOLDOWN_HOURS`): they reopen from about 02:15–02:21Z on 2026-09-29. The metaverse
placeholders incident (template-only, MAINTENANCE_GREEN by trusted policy) can reopen immediately after re-enable.

## Owner checkpoint (open)

Nothing below can be done by the external agent without a credential passing through its context.
Set secrets as Railway **sealed** variables (they cannot be read back, even by tools).

- **A. Review and merge PR #66** (RED trusted base). Without it: live diagnosis fails
  (`turn_budget`), release-control crash-loops on `main`, rollback is broken against the live Railway
  schema, there is no preview (every release refuses), and the scheduled probe's token expires after one hour.
- **B. Attestation key.** Railway → AgentNet → staging → Settings → Shared Variables → New:
  `MAINTENANCE_ATTESTATION_KEY` = the output of `openssl rand -hex 32` run on your own machine, pasted directly.
  The agent then wires `${{shared.MAINTENANCE_ATTESTATION_KEY}}` into society-worker and release-control
  (references only; it never reads the value). Do not add it to any production service.
- **C. Browser-probe ingest account.** Choose an email and a password (12+ characters, with upper case, lower case and a digit). Then:
  - GitHub → repo Settings → Secrets and variables → Actions → New repository secret:
    - `MAINTENANCE_INGEST_URL` = `https://registry-staging-145d.up.railway.app/v1/maintenance/observations/browser`
    - `MAINTENANCE_INGEST_EMAIL`
    - `MAINTENANCE_INGEST_PASSWORD`
  - The same email and password on Railway → staging → staging-validator (sealed): `MAINT_INGEST_EMAIL`, `MAINT_INGEST_PASSWORD`.
  - The agent then runs `VALIDATOR_SCRIPT=maintenance_ingest_account.py`: register, verify, grant `event_producer` through the operator API.
- **D. GitHub App "AgentNet Maintenance Release".**
  - github.com → Settings → Developer settings → GitHub Apps → New GitHub App.
  - Homepage: the repository URL. Webhook: untick *Active*.
  - Repository permissions: Contents *Read and write*, Pull requests *Read and write*, Checks *Read-only*, Metadata *Read-only*. Nothing else: no Administration, Secrets, Workflows or Environments.
  - Where it can be installed: *Only on this account*.
  - Create it, then *Generate a private key* (a .pem downloads).
  - Install it: *Only select repositories* → `vansyson1308/agentnet`.
  - Do not add it as a ruleset bypass actor.
  - On Railway → staging → release-control set:
    - `MAINTENANCE_RELEASE_GITHUB_APP_ID`;
    - `MAINTENANCE_RELEASE_GITHUB_INSTALLATION_ID` (the number in the installation URL);
    - `MAINTENANCE_RELEASE_GITHUB_PRIVATE_KEY_PEM` = the .pem contents (sealed).
  - The start command writes the key to a mode-0600 file and removes the variable before Python starts.
- **E. Railway tokens.** Railway → AgentNet → Settings → Tokens:
  - a token for environment **production** → release-control `MAINTENANCE_RAILWAY_TOKEN` (sealed);
  - a second token for environment **staging** → release-control `MAINTENANCE_PREVIEW_RAILWAY_TOKEN` (sealed).

  The agent has already set every non-secret identifier (project, environments, service ids, origins, preview mapping).
- **F. release-control → Settings → Deploy → enable *Wait for CI*.** This dashboard-only setting does not
  persist through the connector.

After A–F the agent resumes this same mission:
1. point release-control back at `main`;
2. provision the ingest account and prove a scheduled probe ingest;
3. re-enable autonomy and cognition, then GREEN promotion only after a live diagnosis succeeds;
4. dark-boot, then preflight the live release provider;
5. prove rollback on staging, never production;
6. then the production proofs.
