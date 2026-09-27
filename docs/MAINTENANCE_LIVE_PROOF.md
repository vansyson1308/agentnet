# Maintenance OS — live proof record

**Verdict at merge of this change: NOT LIVE.** The Maintenance OS is built, tested deterministically
and merged dark (every switch `false`). None of the live graduation proofs has run yet. The terminal
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

| # | Proof | Status | What it needs |
|---|---|---|---|
| 1 | Maintenance Kernel live on staging with real DeepSeek (no scripted model) | **NOT RUN** | owner merges this change; staging society-worker redeploys; set `MAINTENANCE_MONITORING_ENABLED=true`, then `MAINTENANCE_AUTONOMY_ENABLED=true`, `MAINTENANCE_COGNITION_ENABLED=true`, `MAINTENANCE_GREEN_PROMOTION_ENABLED=true` |
| 2 | Real FUNCTIONAL ROUTING incident ingested and repaired by the Society (expected AMBER → owner merge) | **NOT RUN** | #1; the incident opens from the live monitor within two probe cycles |
| 3 | Real EXPERIENCE QUALITY incident (contrast / raw capability dicts / banner) ingested from deterministic browser probes, repaired by the Society as MAINTENANCE_GREEN if trusted risk says so, auto-merged, auto-released, verified → `AUTO_REPAIRED` | **NOT RUN** | #1 + a deep-tier probe schedule posting to the ingress + #5 |
| 4 | AMBER safe escalation → owner approval → automatic continuation | **NOT RUN** | #2 |
| 5 | Release Controller live: exact SHA, production PR, production CI, merge, Railway deployment, public validation (provider API evidence) | **NOT RUN** | owner provisioning in `MAINTENANCE_RELEASE.md` (Release App, release-control service, production-scoped Railway token, attestation key, release-preview surface) |
| 6 | Railway rollback mechanism proven against preview/staging via schema discovery | **NOT RUN** | #5's preview surface; a controlled bad release there (never production) |
| 7 | Kernel restart mid-case completes the case | proven deterministically; **live NOT RUN** | #1 |
| 8 | Release-credential leak audit across logs/events/context | **NOT RUN** (no credential exists yet) | after #5 |

## Graduation invariant

At every live checkpoint record the output of the operator status page's `nothing_stranded`
(`reconciler.stranded_count`). The deterministic suite proves it is 0 across every scenario above; the
live value has not been recorded because the kernel has not run live.

## Why the external coding agent could not finish the live proofs

Claude built only the Maintenance OS infrastructure (mission items 78, 156–157). The live proofs need
(a) the owner to review and merge a constitutional (RED) change to the trusted base — the repository's
own law forbids the builder of the change from approving it; (b) credentials and infrastructure that
only the owner can create (a GitHub App registration, a production-scoped Railway token, a release
preview environment); and (c) a real Society repair authored by the Society, not by Claude.
