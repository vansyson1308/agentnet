# Autonomous company mode

AgentNet's Society runs the product like a small company, **on the one existing control plane**: the Society worker, typed intents, policy, grants, approvals and promotion. No new loop, no new agent and no new authority are introduced. Decision: [ADR-0009 D15–D16](adr/0009-a2a-v1-federation.md).

## 1. The daily cycle

| Step | Mechanism |
| --- | --- |
| **Observe** | At most one scheduled `company.cycle` event per UTC date (UNIQUE partial index), at or after `SOCIETY_COMPANY_CYCLE_HOUR_UTC`. It carries a bounded **evidence bundle** (see the list below the table). Aggregates only; no user identities or content |
| **Diagnose / Prioritize** | The event wakes the **Governor** (product/strategy) and the **Scout** (research, customer insight, growth). They may conclude "no high-value change" |
| **Build** | The existing chain: proposal → Architect → Builder (isolated worktree) |
| **QA / Security** | Existing QA and Security roles, trusted risk classifier |
| **Promote** | Existing promotion controller: GREEN-only auto-merge law, merge freezes |
| **Evaluate** | Existing Evaluator and fitness engine, plus the product/A2A fitness below |
| **Learn** | Memory and settled outcomes: `changes_proposed` or **`no_high_value_change`** (a valid, recorded outcome) |

The evidence bundle contains:

- marketplace task outcomes;
- A2A inbound states and audit results;
- the federation catalog and outbound call results;
- signup funnel counts;
- denied intents;
- open incidents.

Operators can also start a cycle immediately: `POST /v1/society/company/cycles` or `python -m app.society.company cycle`. There is no waiting for tomorrow.

## 2. Company functions mapped to existing roles

| Function | Role |
| --- | --- |
| Product / strategy | Governor |
| Research, customer insight, growth | Scout |
| Engineering | Architect + Builder |
| QA | QA |
| Security | Security |
| SRE / reliability, finance / economics | Evaluator |

## 3. Anti-busywork and portfolio caps

These apply while `SOCIETY_COMPANY_CYCLE_ENABLED=true`:

- `SOCIETY_COMPANY_MAX_ACTIVE_HYPOTHESES` (default 3) limits the Society hypotheses **still being pursued**. A new one is refused until one concludes.
  `company.portfolio_accounting` decides what holds a slot. It reads durable rows and never rewrites a proposal's status:
  - **concluded**: CONVERTED_TO_TASK whose every linked candidate ended (rejected / failed / abandoned, or READY with a merged / rejected / superseded promotion), or whose converted task reached a terminal state. No slot.
  - **shelved**: APPROVED and untouched for `SOCIETY_COMPANY_HYPOTHESIS_SHELF_HOURS` (default 72, minimum 24). No slot, but it stays APPROVED and visible; converting it later makes it active again.
  - **active**: everything else (PROPOSED, UNDER_REVIEW, a fresh APPROVED, work in flight, a conversion with no linked work found).

  Why: until 2026-09-26 the cap counted every row in an open *status*. A proposal stays CONVERTED_TO_TASK after its candidate merges, and nothing ever moves an unstarted APPROVED one. On staging the portfolio read "6 active (cap 3)" with 2 concluded and 3 untouched for 6–7 days, so the Scout's proposal for a critical public-surface regression was refused and no role could "conclude one". The cap was not raised.
- `SOCIETY_COMPANY_MAX_HIGH_RISK_INVESTIGATIONS` (default 1) limits open RED candidates. New code changes are refused until one finishes.
- The existing change budgets, evidence rules for signal-driven proposals and duplicate suppression still apply.
- There is **no quota** of commits, PRs or features. The cycle's instructions say so, and `no_high_value_change` is recorded as a successful outcome.

## 4. Product and A2A fitness

`company.a2a_fitness(evidence)` gives the Evaluator:

- inbound interop success rate;
- federated task success rate;
- healthy remote agent ratio;
- quarantined/blocked count;
- inbound refusals;
- marketplace task success.

It never optimizes raw network size or spend.

## 5. External agents as vendors

Using a remote A2A agent is a Governor intent:

- `REQUEST_A2A_TASK` is **approval-gated**;
- it has daily, per-correlation and circuit-breaker budgets;
- remote results arrive as untrusted events.

See [A2A_FEDERATION.md §6](A2A_FEDERATION.md). External agents cannot approve intents, change policy, credentials, the risk classifier or budgets, merge anything, deploy, or touch GitHub, Cloudflare or billing.

## 6. Governance

- **Kill switch:** `SOCIETY_RUNTIME_ENABLED=false` stops cycles, cognition, intents and code work. The marketplace and the A2A API keep serving; they are separable.
- **Incident freeze:** `POST /v1/society/incidents` opens a freeze. `promotion.merge_freeze_reasons` then reports `incident_freeze_open(n)`, and autonomous merge authority stops. **Only an operator** lifts it (`POST /v1/society/incidents/{id}/lift`); no intent, model or external agent has a path there.
- **Cost governor:** the existing daily model budget, per-agent budgets and per-correlation cost cap, plus the A2A call budgets.
- **Operator status report:** `GET /v1/society/company` (operator) or `python -m app.society.company status`. It covers:
  - mode flags;
  - recent cycles and outcomes;
  - portfolio versus caps;
  - evidence and fitness;
  - candidates and promotions by status, release-ready candidates;
  - budgets;
  - open incidents.

  No secrets.

## 7. Production authority stays trusted

- The Society has **no** production deploy flag: `production_deploy_enabled` is hard `False`.
- It has no Railway, Cloudflare or GitHub-bypass credential and no billing authority.
- The production Society runtime is refused by configuration validation (`ENVIRONMENT=production`).
- Production releases remain the trusted operator boundary (`deploy/production/release.py`).
- Normal green product evolution happens on staging and `main`. Constitutional or RED changes need owner approval.

## 8. Maintenance is not a company hypothesis (ADR-0010)

A proven violation of an existing product contract is a **Maintenance Incident**, handled by the
Maintenance OS (`docs/MAINTENANCE_OS.md`): it never takes a portfolio slot, is never deduplicated
against proposals, and never waits for the 01:00 cycle. The company cycle keeps the innovation lane
(features, experiments, strategy). An exhausted availability error budget or an active P0 repair
freezes innovation promotion; maintenance and security repairs continue.

## 9. From an approved ticket to a candidate

A ticket (`society/tickets.py`) says why work exists: an active objective, a key-result metric and a
proof. The owner approves the daily plan, `company.ticket_approved` opens one design story per
ticket, and the Architect designs it with `REQUEST_CODE_CHANGE`.

**Bench tickets.** A backlog bench ticket ("Builder harness: deliver dev task X (k/3 runs)") names
what failed, not what to change. The change it needs is to the **builder harness**
(`backlog.HARNESS_PATHS`), so that tasks like X are delivered. X itself is already solved on `main`.

- **Work packet** (`society/work_packet.py`, deterministic). The Architect's context carries
  `engineering.company.ticket` and `engineering.company.work_packet` for the story the approval
  opened. The packet holds:
  - the evidence from the latest main bench report that ran X: the result class of each run, plus
    turns, test runs, patches and the last tool codes (`scripts/bench` stores these per run for dev
    tasks only);
  - the target: the harness file and function that the dominant failure class implicates, refined
    by the last tool code (`TARGETS`, `CODE_TARGETS`);
  - the proof rule and the regression tests.

  `GET /v1/society/company/tickets` shows the structural part. Holdout tasks never get a packet.
- **Spec rules** (executor, before the meaning gate):
  - `files_allowed` holds at least one harness file, and anything else only under `tests/`;
  - `kind` is `code`;
  - the ticket's `bench:X` is always an acceptance criterion. The regression tests are filled in
    when the spec names no pytest target.
  - A ticket id passed as `proposal_id` is refused, with the fix in the error message.
- **Proof** (`engineering/bench_proof.py`). After the pytest criteria pass, QA re-runs X three times
  with `scripts/bench/run.py` on the candidate harness. This is the `BENCH_HARNESS_ROOT` path, with
  the running revision's judge and task list.
  - It passes when X is delivered on at least 2 of 3 runs **and** on more runs than in the baseline
    (the latest main report).
  - The child process runs model-authored code, so it never holds the model key. It reaches the
    provider through a loopback relay that has a per-run token, accepts only `/chat/completions`
    and caps the number of requests. It gets no database, GitHub or signing value.
  - The run is bounded by `SOCIETY_BENCH_PROOF_BUDGET_USD` and `SOCIETY_BENCH_PROOF_TIMEOUT_SECONDS`.
    Its cost is charged to the QA run and the ticket, and its numbers ride `qa_report.bench_proof`.
  - The Builder runs only the pytest targets.

**Design failures** (`tickets.record_design_failure`).
- **Reason on the ticket.** When a `REQUEST_CODE_CHANGE` fails, its error goes on the ticket's
  `reason` as `design failed (n/2): <error>`. This covers live intents and intents resumed after an
  operator approval. The reason shows in `GET /v1/society/company/tickets` and in the validator's
  `company` step.
- **Back to proposed.** The second failure since the ticket's latest approval returns it to
  `proposed`: the plan is cleared and `company.ticket_returned` is emitted, so the owner can re-plan
  it. Re-approving it in a new plan wakes the Architect again, with a fresh count.
- **Stale design.** An approved ticket with no design activity for 6 h counts as one failed
  attempt per quiet period. A ticket is never silently stuck in `approved`.
- **Capacity refusals** (`portfolio full`, `change budget exhausted`) are recorded as `waiting: …`
  and not counted.
- **Meaning-gate refusals** close the ticket `refused`, as before.

**Read budget.**
- The Architect gets `SOCIETY_TICKET_READ_BUDGET` (default 6) repository reads per design story.
  The context shows `engineering.company.ticket.read_budget` as `{used, max, left}`.
- Past the budget, reads are refused and count as a failed design. A refused read wakes nobody, so
  the story ends there and one ticket cannot burn the role's hourly run cap.
- Instead of guessing, the Architect may answer `TICKET_NEEDS_INFO {ticket_id, missing[], detail}`.
  It is a LOW intent, Architect only, and applies only to the ticket its own story designs. The
  ticket returns to `proposed` with `needs_info [...]: detail` for the owner.

**RED tickets.** A bench ticket changes the builder harness, which is RED trusted base.
- **Plan ranking.** The daily plan holds at most as many RED tickets as there are free high-risk
  slots: `SOCIETY_COMPANY_MAX_HIGH_RISK_INVESTIGATIONS`, minus open RED candidates, minus approved
  RED tickets still waiting for a design. Non-RED tickets fill the rest, so the plan stays
  buildable. `ranking[].risk` says which is which, and an empty plan says why.
- **PR body.** A RED candidate's PR body carries a `### Bench verdict` section: candidate x/3
  against the main baseline y/3, the runs, the cost and the rule.
- **Owner merge queue.** The PR is listed in `GET /v1/society/approvals` (under
  `candidates.owner_merge`), in `GET /v1/society/company` (under `owner_merge_queue`) and in the
  validator `company` step (K07). It is never auto-merged; the owner merges it.
