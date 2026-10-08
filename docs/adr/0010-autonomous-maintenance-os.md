# ADR-0010 — Autonomous Maintenance OS

- Status: accepted (implementation merged dark; live graduation NOT yet earned — `docs/MAINTENANCE_LIVE_PROOF.md`)
- Date: 2026-09-27
- Related: ADR-0001 (Society runtime), ADR-0004 (self-development), ADR-0008 (production release boundary), ADR-0009 (A2A + company mode), `docs/MAINTENANCE_OS_BASELINE.md`
- **Trusted base.** This ADR, `services/registry/app/maintenance/**`, the desired-state registry, the detectors and the verification tests it names are RED in `society/risk.py`. The Society may diagnose defects in them and *prepare* a change; it may never edit this ADR in the same change it evaluates, and never auto-release a change to the kernel, the policy, the attestation validator, the release controller or the rollback controller.

## Context

On 2026-09-27 the Society had public-surface eyes, a GREEN evolution loop and an exited external coding agent, but no production defect had ever been repaired autonomously. The open dashboard defect showed why: the maintenance "workflow" was a chain of model decisions and events (Scout proposes, Governor approves, Architect specifies, Builder builds, QA/Security review, Evaluator promotes). Every link could strand it, and eleven PRs (#51–#62) each patched one way it stranded: portfolio slots, false memories, signal coverage, swallowed read results, context truncation, an immutable spec that was too narrow, a concluded proposal that "covered" the defect, a wake the loop breaker swallowed.

The cause is architectural: **agents were the maintenance state machine.**

## Decisions

### D1 — Agents are cognitive workers; trusted code is the control plane

The model may diagnose, hypothesise, reason about root cause, plan, author patches, review code and security, and explain. It may not own workflow state, retries, deduplication, timeouts, rescope authority, release eligibility, credentials, rollback authority, risk policy, error budgets or evaluation criteria. Those live in deterministic code in `app/maintenance/`.

### D2 — Desired-state contracts

`app/maintenance/desired_state.json` is the ONE registry of desired product state: `public_surface`, `browser_experience`, `api`, `a2a`, `human_auth`, `email`, `economic_invariants`, `security_invariants`, `performance_budgets`, `dependency_posture`. Each names its SLIs, its trusted detectors, the trusted verification tests that judge a repair, and whether autonomous repair is allowed at all (money, security, email and dependency posture: **no** — they escalate). The public-surface contract (`society/public_surface_contract.json`) stays authoritative for its items.

### D3 — Observation model and taxonomy

Collectors are deterministic (surface HTTP monitor, Playwright browser probe, watchdog, CI, database invariants). They record `maintenance_observations`: SLI, target, source, collector version, trust class, ok, digest, structural payload. Model text is never an observation. Incident classes, priorities (P0–P3), trust classes, activity kinds, risk classes and release statuses are closed enums (`taxonomy.py`); there is no free-text category.

### D4 — Maintenance incidents and fingerprints

A violated contract is a `MaintenanceIncident`, not a hypothesis. It never enters the innovation portfolio, proposal deduplication or strategy review. Fingerprints are sha256 over (target, class, desired-state reference, failure class, normalised path) — stable across timestamps, request/deployment ids and instance ids; different failures differ (`fingerprint.py`, tested). One open incident per fingerprint (partial unique index). A recurrence of a recovered fingerprint opens a linked incident (`recurrence_of`); a fingerprint that keeps coming back within 7 days is raised one priority level.

### D5 — Coverage, memory and facts

An incident is *covered* only when an active `RepairCase` exists for it (`incidents.is_covered`). No memory row, proposal, chat message or model statement can cover, resolve or suppress an incident. Trusted facts come from executed actions, domain events, CI, provider state, monitors and database invariants; everything a model writes is stored as a `model_hypothesis` artifact. Terminal cases derive `maintenance_knowledge` rows from durable rows only (root cause labelled as the accepted hypothesis; no chain-of-thought).

### D6 — Repair cases, the executable state machine and reconciliation

One incident has at most one active `RepairCase` (partial unique index; a resumable escalation still covers). The state machine (`state_machine.py`, generated into `docs/MAINTENANCE_STATE_MACHINE.md`) declares for every non-terminal state its allowed transitions and actors, timeout, poll cadence, try budget, deadline recovery and exhaustion target; `assert_total()` proves at import that every state exits. The database refuses a live case without `next_action_at`/`deadline_at`.

The **Maintenance Kernel** (`reconciler.py`) reconciles: every cycle it claims due cases (`FOR UPDATE SKIP LOCKED` + lease), asks what the persisted state is and what should happen next, and does it idempotently. Events are not needed at all; a swallowed event, a crash, a Redis/Postgres blip or a restart cannot strand a case. Results of long activities are applied only if the lease is still ours (fencing). Recorded facts beat timeouts. Controller downtime shifts live deadlines on restart. The stall detector raises a `CONTROL_PLANE` incident for any case overdue without a lease; a separate watchdog (in the release-control process) watches the kernel heartbeat, queue lag and the stranded count.

### D7 — Cognitive activities

Model calls are typed activities (`activities.py`): `DiagnoseIncident`, `DesignRepair`, `AuthorPatch`, `ReviewPatch`, `SecurityReview`, `ExplainEscalation`, `DraftPostmortem`. Each has an input description, a strict output schema, a turn budget, a token cap, a role and a tool list. One try is a bounded tool loop over read-only, paged repository tools (and, for `AuthorPatch`, patch/test/diff tools bound to the attempt's worktree). Failures are classified (`timeout`, `rate_limit`, `provider_error`, `invalid_json`, `invalid_output`, `partial`, `empty`, `turn_budget`, `cost_budget`); an invalid answer gets one corrective turn. Each try is a durable `repair_activities` row committed *before* the model is called; a crashed try is marked `abandoned` and counts. The kernel decides retries (`MAINTENANCE_ACTIVITY_MAX_TRIES`) and, when exhausted, escalates — never strands. The live backend is the Society's configured OpenAI-compatible provider (same credential boundary); scripted backends are labelled `scripted` on every row and are never live evidence.

### D8 — Repair plans, rescope and the patch harness

Plans are immutable `repair_plan_revisions` (update refused by trigger). A rescope creates revision N+1 (it may add files; it never silently drops scope) — no proposal restart, no Scout, no Governor. The contract's own verification tests are always acceptance tests; the model may add existing tests, never remove the trusted ones, and never put an acceptance test in `files_allowed`. Trusted policy re-classifies every revision; crossing GREEN→AMBER or →RED is recorded and changes the outcome, never silently widens authority.

The Builder edits through a typed `PatchSet` (`replace_exact`, `insert_after`, `insert_before`, `create`, restricted `delete`): all-or-nothing, unique anchors only, stale base refused, protected and out-of-scope paths refused before any write. Reads are paged with explicit `total_bytes`/`total_lines`/`next_line`/`truncated` (`repo_tools.page_text`); structural tools map routes, template `url_for` references, symbols, references and test ownership. Within one attempt the Builder iterates read → patch → targeted test → patch. A QA or Security failure is input to the next attempt of the same case. Every attempt's worktree is keyed by `(case, attempt)` and reset to base on (re)start, so a replay cannot double-apply.

### D9 — Maintenance risk policy

`policy.classify_patch` builds on the base classifier (`society/risk.py`) and adds: evaluation paths and the kernel are RED; changing product + its judge in one change is **evaluation laundering** (RED); money semantics in the diff → RED; auth/session semantics → at least AMBER; infra/DNS → RED; oversized diffs/scopes → AMBER; NEVER findings and **anti-reward-hacking** findings (catch-all exception, url-build fallback, `#` placeholder fallback, rewritten error handler, hidden error banner, removed error message, disabled feature) → `CONSTITUTIONAL` → `POLICY_REFUSED`. `MAINTENANCE_GREEN` is only the base-GREEN class (templates, static assets, docs) under all of those checks. Wallets, escrow, transactions, auth core, secrets, risk/policy, trusted evaluation, GitHub rules, Cloudflare, the release controller and destructive migrations are never GREEN.

### D10 — Browser and experience quality

A deterministic Playwright probe (pinned Playwright 1.56 / Chromium; pinned axe-core 4.10.3) checks critical journeys in-browser and returns only structure: rule id, page, safe path, selector class, count, numbers. Rules: navigation/final path, redirect loop, console errors, JS exceptions, failed same-origin requests, placeholder/dead links, WCAG 2.2 AA text contrast from computed styles, form labels, raw structured values, unexpected error banners, layout overflow at phone and desktop widths, critical content, keyboard focus, axe WCAG A/AA, performance budgets. Visual-test mode disables animations and uses stable fixture data; production uses semantic assertions, never pixel diffs. Page text never reaches the Society. The deep tier reports through a strict, structural, event-producer-only ingress.

### D11 — Maintenance lanes

Incident maintenance is event-driven and independent of the 01:00 company cycle and of the innovation portfolio. Triage lanes: money/data invariants freeze mutations and escalate P0 (data repair stays owner-controlled); external-dependency and runtime-unavailability incidents are escalated, never "fixed" by code without evidence; contracts marked non-autonomous escalate; a `CONTROL_PLANE` incident is never self-repaired.

### D12 — Priority, queue and model budget

P0/P1/P2/P3 are derived deterministically (class × severity × journey). The maintenance queue has its own capacity (1 urgent + 2 routine; urgent may borrow a routine slot, never the reverse). Maintenance has its own model budget with a P0/P1 reserve and a per-case cap; agents cannot raise either.

### D13 — SLOs and error budgets

SLIs/SLOs (`slo.py`): availability 99.5%, API readiness 99.5%, A2A discovery 99%, auth journeys 99%, public pages 98%, browser journeys 95%, each over 7 days with a minimum sample count; repair latency P0 ≤ 6 h / P1 ≤ 24 h at 90% (reported). With an availability-class budget exhausted, innovation promotion freezes and only P0/P1/security repairs (and rollbacks) release. An active P0 case also freezes innovation merges.

### D14 — Incident freezes and the repair exception

Availability/security/money incidents open an operator-lifted incident freeze (ADR-0009 D15 law unchanged). The promotion controller lets exactly one kind of change through a freeze: the maintenance repair whose case is linked to the frozen incident, whose changed files are inside its immutable plan revision, and whose trusted class permits merging (`policy.repair_exception`). Otherwise a freeze would deadlock its own recovery.

### D15 — Release attestation

A GREEN (or owner-approved AMBER) repair on `main` gets an immutable attestation (incident, case, base/head/merged sha, diff digest, changed files, risk decision, CI, QA, Security, staging and browser proof, contract proof, SLO state, known-good production sha, owner approval, timestamp), canonical-JSON hashed and HMAC-signed (`MAINTENANCE_ATTESTATION_KEY`). The signature makes tampering detectable; safety comes from D16's recomputation.

### D16 — The Maintenance Release Controller

A deterministic, model-free process (`python -m app.maintenance.release_worker`) in its own service boundary (`release-control`) holding the ONLY production release credentials: a dedicated Maintenance Release GitHub App (contents/pull-requests write, checks read; no admin, no ruleset bypass, no secrets, no workflows) and a production-scoped Railway token. It refuses to start with a model or Society credential in its environment and never imports cognition. It recomputes everything: attestation digest/signature, case state, exact SHA on `main`, `production` ancestry, the production..sha diff equals the attested repair (no unrelated unreleased change rides along), not truncated, trusted class (its own copy of policy), required checks, owner merge for AMBER (verified on GitHub), freezes, error budget, daily cap (1), provider health and Railway schema discovery. It fails closed.

### D17 — Release flow, exact SHA, service-aware deploy

Verify → record known-good (deployments per service, sha, tree) → release-preview validation of the exact SHA (fail closed when no preview surface is configured) → `release/prod-<sha>` branch → PR to `production` (same rules as humans: required CI, strict, no bypass, no force push) → merge when green with expected head → deploy only the changed services at that exact SHA (adopting a Wait-for-CI deploy Railway already started; triggering `serviceInstanceDeployV2(commitSha)` after a grace period otherwise) → N consecutive healthy public observations → SUCCEEDED and a new known-good record. Only one maintenance release is in flight (partial unique index). If production moved after verification, the release is refused (re-attestation required).

### D18 — Rollback

On a post-deploy regression (an item healthy in the pre-release baseline now failing) or a failed deployment: roll each changed service back to its recorded known-good deployment (`deploymentRollback(id)`), falling back to redeploying the known-good SHA when the image expired; success requires the public desired state restored for N observations. Then a branch-reconciliation PR whose tree IS the known-good tree, and a `rollback_parity` release freeze the controller lifts only when the production branch tree equals it. A failed rollback is P0: `ROLLBACK_FAILED`, an owner-only freeze, `SAFELY_ESCALATED`, no retry storm. Production databases are never downgraded; a release with a migration is never MAINTENANCE_GREEN.

### D19 — Owner authority and operator experience

Owner decisions resume the persisted case (no model re-call): merging the AMBER PR on GitHub *is* the approval (the kernel's resume sweep detects it); `promotion_disabled`/`release_disabled` escalations resume on request; refusals make an escalation final; release freezes open/lift. One operator status page (`/v1/maintenance/console` over `/v1/maintenance/status`), operator-only APIs for incidents, cases, releases, error budget, KPIs; a sanitized public `/v1/maintenance/summary`. Kill switches are separate (monitoring, cognition, GREEN promotion, GREEN release) under one master `MAINTENANCE_AUTONOMY_ENABLED=false` that stops new autonomous repairs and releases without touching the public product. Migration `0014_maintenance_os` is additive; it is never downgraded in production.

### D20 — Measurement

KPIs from durable rows: MTTD, MTTR, auto-repair/rollback/escalation/false-positive rates, attempts per incident, model cost per resolved incident, recurrence rate. Toil events (owner approvals/refusals, freeze lifts, manual merges/releases/abandons/memory corrections) are counted; GREEN maintenance toil should trend to zero.

## Constitutional boundaries (unchanged by this ADR)

- The production Society stays OFF. Production maintenance is performed by a deterministic release controller, not a production LLM worker.
- The Society holds no production credential. `SOCIETY_AUTO_MERGE_ENABLED` stays `false` by default; no intent can change it or any maintenance switch or bound.
- AMBER/RED/constitutional changes, billing, new credentials/providers, destructive data actions and DNS/Cloudflare changes remain owner decisions.
- A repair that edits the contract, detector or test that judges it is evaluation laundering (RED).

## Consequences

- Maintenance no longer depends on the proposal portfolio, memories, events or the loop breaker; the one-off recovery paths added in #51–#62 remain for the innovation lane only (historical replay tests: `tests/society/maintenance/test_historical_replay.py`).
- The dashboard product repair still belongs to the Society; this ADR builds only the machinery that lets it happen and be released safely.
- A live graduation requires owner actions this ADR cannot perform (Release App registration, release-control service and tokens, a release-preview surface, enabling the switches). `docs/MAINTENANCE_LIVE_PROOF.md` lists them and records honestly that the terminal verdict is not yet earned.
