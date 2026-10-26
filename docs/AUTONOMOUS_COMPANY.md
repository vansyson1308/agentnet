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
proof. The owner (or, after the owner's window, the board: §10) approves the daily plan, `company.ticket_approved` opens one design story per
ticket, and the Architect designs it with `REQUEST_CODE_CHANGE`.

- **A ticket story offers only the design.** In a design story (or its design review's round) the
  Architect is offered `PROPOSE_DESIGN`, `REQUEST_CODE_CHANGE`, `TICKET_NEEDS_INFO` and the ticket's
  budgeted repository reads. Any other intent is denied (`context._story_permissions`; the grant is
  narrowed, never widened). Staging 2026-10-14 showed why: a6dd4579's design carried a `task_id`,
  and a ticket is not a task session. A ticket design that names a `task_id` is refused, and the
  bench packet's field is now `bench_task`.
- **Spec gate before any build.** Every pytest file in `acceptance_tests` must exist on the base
  (`SOCIETY_REPO_ROOT`) or be listed in `files_allowed` (a new test the Builder writes). Otherwise
  the request is refused with `acceptance file missing and not allowed: <path>`, and no candidate
  is created. Staging 2026-10-14 showed why: cb82c1a7 named a test file that did not exist and
  burned 3 builds. Each of these refusals gives the Architect one corrective turn in the same
  story (`code_change.spec_rejected`, targeted at the Architect), so it can re-design.

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

## 10. The board: the owner's standing delegation for the daily plan

Without it the company stands still every day until the owner opens the plan: on staging the
2026-10-13 plan was still `awaiting_owner` 14 hours after the cycle, and earlier plans waited
5–17 hours. The board (`tickets.board_decide`, deterministic, no model) removes that wait.

- **Off by default.** `SOCIETY_COMPANY_BOARD_APPROVAL_HOURS=0` keeps every plan waiting for the
  owner. Set it to N (1–72) on the society-worker to delegate.
- **The owner decides first.** A plan is the owner's for N hours after the cycle builds it. An
  owner approval or rejection inside that window is final; the board never touches it.
- **Approve only.** After the window the board approves the plan exactly as the owner would
  (`company.ticket_approved`, `actor_type=system`, `payload.approved_by=board`). It never
  rejects, never edits a ticket, the gate, the ranking, a budget or the RED merge law: a RED
  candidate's PR still waits in the owner merge queue (§9).
- **Healthy only (fails closed).** It holds, and writes why on the plan's `board_note`, while
  an incident freeze is open, a society run went DEAD in the last 6 h, or the daily model
  budget is spent. The note is recomputed on every board pass and when an operator lifts a
  freeze (`tickets.refresh_board_note`), so it never names a lifted freeze. A freeze that outlived
  its cause (`company.OUTLIVED_FREEZES_SQL`: its maintenance incident recovered or closed, or for a
  `public_surface` freeze a `public.surface.recovered` event after it at least 6 h ago and the
  latest monitor check green) is named on that note and in `GET /v1/society/company`
  (`freezes_outlived`) the same day, so the owner sees it; it is never lifted automatically.
- **Isolated.** The worker runs each ticket controller (outcomes, relabel, close, stale-design
  sweep, board, design-review sweep) in its own transaction and logs a failure as
  `company controller step <name> failed`: a broken step can neither skip the board nor roll
  back its hold note.
- **Visible.** `GET /v1/society/company/plans` shows `decided_by` (`owner` | `board`) and
  `board_note`; `GET /v1/society/company` shows `mode.company_board_approval_hours`.
- Schema: `SOCIETY_COMPANY_BOARD_SQL`, migration `0019_company_board`.

## 11. The design review: the company meets before it builds

Until now one agent decided alone what to change: the Architect read the ticket and designed.
The other roles only saw the result after it was built, and `agent_chat` stayed empty (0 rows in
every staging audit). The design review (`society/design_review.py`) is the meeting in between.

| Step | Who | Typed intent | What the runtime enforces |
| --- | --- | --- | --- |
| Request | runtime | — | an approved ticket (owner or board) opens a review in a **fresh story** (`design_review.requested`); owner/incident tickets skip it |
| Propose | Architect | `PROPOSE_DESIGN {review_id, approach, files, proof, risks, acceptance_tests, test_first, unchanged}` | only in the review's current round; the spec gate first |
| Review | Scout (evidence, value), QA (provability), Security (risk) | `REVIEW_DESIGN {review_id, stance: support\|concern\|object, points}` | once per role per round; only these three roles |
| Decide | Governor | `DECIDE_DESIGN {review_id, verdict: go\|revise\|drop, rationale, conditions}` | a Security `object` bars `go`; round 2 is the last (no `revise`) |
| Design | Architect | `REQUEST_CODE_CHANGE` | `go` emits `company.ticket_approved` in a **fresh** design story with the minutes; the candidate spec and the PR body carry them (structural only) |

- **A conversation.** Every proposal, review and decision is also an `AgentChat` message in the
  review's thread (`thread_id` = review id).
- **Every role is called by name.** Each stage change emits one targeted system event per role
  that must act (Architect to propose; Scout, QA and Security to review; Governor to decide), in the
  review's story at depth 0. Nobody else is woken, and each role gets its own run. An hourly run cap
  delays a review turn by 5 min; it never drops it.
- **A timeout is never approval** (`design_review.sweep`, every controller pass). Only the Governor
  says `go`. No proposal within 2 h → the Architect is woken once more; no proposal after that
  → the ticket returns to `proposed` with the reason. Reviews missing after 2 h → the Governor
  decides with what it has, and its wake and context say `missing: ...`. No decision within 2 h →
  the ticket returns to `proposed`. `drop` refuses the ticket with the Governor's rationale.
  Staging 2026-10-14 showed why: two reviews passed as `go` by timeout with one message between them.
- **Bounded.** Fresh stories keep the loop-breaker depth of the design story unchanged; at most
  2 rounds; each contribution is one LOW intent under the role's existing run and budget caps.
- **Authority unchanged.** The review decides only whether and how a ticket is designed. It cannot
  merge, deploy, change a grant, budget or the risk tier; QA, Security and the promotion controller
  still judge the built candidate.
- **Spec gate before the meeting** (`society/spec_gate.py`, deterministic). A `PROPOSE_DESIGN`
  that cannot become READY is refused before any review, and the Architect is re-woken with the
  structural reasons (`gate` in its wake, at most `GATE_RETRIES` times per round, then the ticket
  returns to `proposed`). Rules: every `acceptance_tests` entry exists on main or is a `test_first`
  file the Architect writes in the design (new `tests/**/test_*.py`, at most 2, never in `files`);
  no acceptance file is in `files` (the Builder never writes its judge); a dashboard template must
  be rendered by a view, and that view is in `files` or named in `unchanged` with the reason; no
  file on a risky surface (`qa.RISKY_PATH_RE`: the Security static scan fails any change there).
  Staging 2026-10-26 lost 4/4 candidates to exactly these faults.
- **Test first.** The executor pins the approved design's `test_first` files (sha256) from the
  persisted minutes, never from the model, and commits them on the candidate branch before the
  build; the harness resets to that commit. A test-first that already passes on base is refused
  (it cannot judge the change). QA exempts only a byte-identical pinned file from the allow-list and
  self-judging checks (`test_first_intact`); a Builder edit to it fails QA.
- **Visible.** `GET /v1/society/company/reviews` (operator: minutes, positions, history) and
  `mode.design_review_enabled` in `GET /v1/society/company`.
- Off by default: `SOCIETY_DESIGN_REVIEW_ENABLED=false`. Schema `SOCIETY_DESIGN_REVIEW_SQL`,
  migration `0020_design_review`.

## 12. Growth supply: product work that needs nobody to file it

Until now every supplied ticket came from the bench (RED harness work), an incident or an owner
issue: on staging 11 of 14 tickets were `Builder harness: ...` and none was about the product,
its users or other agents. `backlog.kr_gap_items` adds the growth key results:

- For each **ACTIVE** objective, every growth KR (`backlog.GROWTH_METRICS`:
  `escrow_tasks_completed_7d`, `a2a_agents_live_card`) that is off target, or has no reading yet,
  is one item per ISO week. It is read only at the company cycle: no event, no heartbeat, no model.
- `tickets.supply` turns it into ONE proposed ticket (`proof: probe:<metric>`, department
  `strategy_product`), never a second open one for the same KR. It is not RED by construction, so
  it is judged from its diff and fits the plan beside the single RED slot; O2 (owner priority 2)
  ranks above O3 bench work.
- Its work packet (`work_packet.METRIC_LEVERS`) says what the metric means, the product levers
  (dashboard pages, docs, SDK, examples) and how it is judged: `outcomes.py` reads the KR at merge,
  +24 h and +7 d (`moved` / `no_effect` / `regressed`).
- The owner turns it on by activating the objective (`POST /v1/society/company/objectives/O2/status`
  `{"status": "active"}`); nothing else changes.

## 13. Proving it runs without the owner

`deploy/railway/phase5_live.py` step `autonomy[:<days>]` (e.g. `PHASE5_PLAN=autonomy:7`) reads
staging read-only and fails when the company waits on a person or a meeting stalls:

- `A01` the oldest undecided daily plan is younger than 24 h (the board decides within its window);
  it prints the board's hold note and every open freeze by source with the status of its
  maintenance incident, so the operator can see which freezes outlived their incident;
- `A02` no design review sits in one stage longer than 7 h, and `go` by the Governor is counted apart
  from `go` by runtime (legacy timeouts). It fails if every `go` came from the runtime;
- `A03` at least one review is a real meeting (>= 4 roles spoke in its thread). For the latest 5
  reviews it prints, per role, the status of the run each wake created and who spoke;
- `A04` / `A05` informational: tickets by kind and source, the federated network, the last directory
  scan (listed/new/refreshed/errors) and the listing requests;
- `A06` informational: each live or recent bench ticket beside its task's newest reports.

Step `freezes:lift-recovered` (e.g. `PHASE5_PLAN=freezes:lift-recovered,autonomy:7`) is an owner
action: as the operator it calls `POST /v1/society/incidents/{id}/lift` ONLY for open freezes
that outlived their cause, by the same SQL as `company.OUTLIVED_FREEZES_SQL` (F01-F04: counts by
source, lifted x/y, none left, the others untouched). It is idempotent. Every freeze it keeps is printed with its reason,
the failing checks of its public-surface anomaly and whether the surface recovered since, so the
operator can decide on it by hand.

### Bench gaps must reproduce

A bench item becomes a ticket only when the task misses the proof bar in each of the newest
`tickets.BENCH_CONSISTENT_REPORTS` (2) main-judged reports that ran it. One report is noise until a
second agrees; a real regression agrees and is supplied. A supplied bench ticket whose gap does not
reproduce closes `closed` (with both scores in its reason) in `close_satisfied`, which also runs
before the board approves a plan; the gate applies the same rule. An owner's own ticket is exempt.

## North star (revenue toward $1M)

The charter's `north_star` (`revenue_usd_total` → $1,000,000) and each KR's `north_star_weight`
rank the plan, reach every role and widen growth supply to the revenue KRs (O4, O5) when
`SOCIETY_NORTH_STAR_ENABLED=true` (off by default). Revenue is USDC platform fees plus
operator-recorded external income (record-only ledger, migration `0022_revenue_ledger`).
With it on, each revenue KR is supplied as one ticket per in-repo play, outside actions wait in the
owner action queue (`PROPOSE_OWNER_ACTION`, migration `0024_owner_actions`), and the plans'
`summary_text` opens with the daily report (`GET /company/report`).
See [NORTH_STAR.md](NORTH_STAR.md) §6.

## Heartbeat precheck (idle wakes cost nothing)

With `SOCIETY_HEARTBEAT_PRECHECK_ENABLED=true` a `society.heartbeat` run is SKIPPED as
`idle: ...` before any context or model call unless its role has something to act on
(`society/wake_precheck.py`): a candidate in a Builder stage, an unreviewed proposal or an
unpromoted READY candidate for the Governor, or a backlog item newer than the role's last
completed run. A role silent for `SOCIETY_HEARTBEAT_MAX_IDLE_HOURS` (default 6) is woken
anyway, so a gap delays work but never strands it. `GET /v1/society/company` →
`heartbeat_precheck` reports idle skips in 24h and the estimated $ saved.

## Throughput: several cycles a day, a bigger plan

`SOCIETY_COMPANY_CYCLES_PER_DAY` (default 1, 1..24) spreads scheduled cycles evenly from
`SOCIETY_COMPANY_CYCLE_HOUR_UTC` to midnight (`company.scheduled_slot`; 4 from 01:00 → 01:00,
06:45, 12:30, 18:15). Each is unique per (date, slot) (migration `0023_company_slots`), settles
into its own plan, and a missed slot is not back-filled. `SOCIETY_COMPANY_PLAN_MAX` (3, ≤ 10) and
`SOCIETY_COMPANY_PLAN_PER_DEPARTMENT` (2) size each plan; builds in flight stay capped by
`SOCIETY_COMPANY_MAX_ACTIVE_HYPOTHESES`. The board, the RED merge law and the budgets are unchanged.
Each cycle wakes the roles that subscribe to `company.cycle` once, so more cycles cost more model calls.
