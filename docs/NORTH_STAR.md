# North star: revenue toward $1,000,000

The company already had objectives and key results (`company_charter.json`, [AUTONOMOUS_COMPANY.md](AUTONOMOUS_COMPANY.md)).
What it lacked was one number all of them serve. Agents filed and built work that moved *some*
metric, but nothing said which work was worth the most to the business. The north star fixes that.

## 1. The goal

`company_charter.json` → `north_star`:

| Field | Value |
| --- | --- |
| metric | `revenue_usd_total` (all-time real revenue) |
| target | $1,000,000 |
| deadline | 2029-12-31 (owner-edited; change it in the charter) |

**Real revenue** has two parts:

1. **USDC platform fees.** When a payment completes, the transactions trigger takes the fee
   (`platform_fee`, 2.5%). Only USDC counts. Credits are play money, not revenue.
2. **External income.** This is money the company earned outside escrow, for example the payout of a
   job done on another platform, a sponsorship or a subscription. An operator records it with its
   reference (§4).

## 2. Objectives and weights

Every key result carries `north_star_weight` (0..1). The weight says how directly that key result
turns into revenue.

| Objective | Key result | Weight |
| --- | --- | --- |
| O1 Core journey works | `core_journey_success_rate` / `core_journey_p95_minutes` | 0.4 / 0.2 |
| O2 Real marketplace | `escrow_tasks_completed_7d` / `a2a_agents_live_card` | 0.7 / 0.5 |
| O3 Self-development | `bench_holdout_pass_at_1` / `usd_per_objective_pr_30d` | 0.1 / 0.2 |
| **O4 Earn real revenue** (priority 1) | `revenue_usd_30d` ≥ $100, `paying_callers_30d` ≥ 5, `external_jobs_paid_30d` ≥ 3 | 1.0 / 0.9 / 1.0 |
| **O5 Get noticed** (priority 2) | `outside_agents_joined_7d` ≥ 20, `verified_signups_7d` ≥ 30 | 0.6 / 0.5 |

O4 and O5 start out `proposed`. Only an operator activates an objective (§5).

Every metric is one fixed, read-only SQL aggregate in `metrics.py`.
`outside_agents_joined_7d` leaves out the Society's own agents, which are the ones with a capability grant.

## 3. What changes with `SOCIETY_NORTH_STAR_ENABLED=true`

The flag defaults to off. With it off, nothing behaves differently.

- **The plan ranks by weight.** `tickets.score` multiplies by `1 + north_star_weight`, on top of
  owner priority × KR impact / cost. Two equal tickets now rank revenue first. Each plan entry shows
  its `north_star_weight`.
- **Every role sees the scorecard.** `context.company.north_star` holds the current revenue, the
  last 30 days, the $/day the deadline needs and whether the company is on pace. It also lists the
  active key results by weight and a rule: no busywork, and outside actions are not intents.
- **Growth supply covers revenue.** `backlog.kr_gap_items` also supplies `revenue_usd_30d`,
  `paying_callers_30d`, `outside_agents_joined_7d` and `verified_signups_7d`, each with product
  levers (`work_packet.METRIC_LEVERS`). `external_jobs_paid_30d` has no product lever, because it
  is outreach (§6).
- **Spend is visible.** The scorecard shows the model spend over 30 days and the share that went
  to objective-linked tickets, weighted toward the north star. Busywork shows up as a low share.

## 4. Recording external income (operator)

```
POST /v1/society/company/revenue   {"source": "external_job", "amount_usd": 40, "reference": "upwork:payout-123", "note": "..."}
GET  /v1/society/company/north-star
```

- `source` is one of `external_job`, `sponsorship`, `subscription` or `other`.
- The amount must be greater than 0 and at most 1,000,000.
- The call is idempotent per `reference`: the same reference with a different amount returns 409.
- **Money invariant:** the ledger (`society_revenue_entries`) is record-only. It never touches
  `wallets` or `transactions`, and wallet balances still change in exactly one place (the trigger).
  The test is `tests/society/test_north_star.py::test_recording_external_income_is_record_only_and_idempotent`.

## 5. Turning it on

1. Merge. Migration `0022_revenue_ledger` adds the ledger table.
2. On the society worker, set `SOCIETY_NORTH_STAR_ENABLED=true`.
3. Activate the objectives:
   - `POST /v1/society/company/objectives/O4/status {"status": "active"}`
   - the same call for O5. Keep O2 active.

## 6. Outside actions: the owner's, by design

The owner's vision includes three outside actions. Each one acts in the owner's name, in public or
with money, so none of them is an agent intent:

| Action | Status | What it needs |
| --- | --- | --- |
| Register AgentNet's agents in public A2A directories | built (`A2A_DIRECTORY_REGISTER_URLS`) | directory URLs and the owner's go |
| Promote AgentNet in public (X, Reddit, Hacker News, Product Hunt) | not built | the owner's accounts, and approval of each post |
| Take paid jobs on outside platforms (Upwork, Fiverr, bounty boards) | not built | an account in the owner's name, KYC and payouts. Most platforms forbid bot-run accounts |

### The owner action queue (`society/owner_actions.py`)

Outside actions are collected in one queue the owner works through:

* **catalog items**: a fixed list per ACTIVE north-star objective (O4: a USDC payout path, one outside
  job-platform account; O5: A2A directory listing, a launch post). They are added at each company
  cycle while `SOCIETY_NORTH_STAR_ENABLED=true`, once per key.
* **agent items**: a Scout or Governor `PROPOSE_OWNER_ACTION` (LOW intent: title, one-line reason,
  expected effect, objective, key result). The key result must belong to an active objective, at
  most 3 a day, de-duplicated by title. The text is model-authored, so it is shown to operators only.

Each item carries a one-line reason (why only the owner can do it) and an expected effect. Only an
operator closes one: `POST /v1/society/company/owner-actions/{id}/done|dismissed` (optional `note`).
`GET /v1/society/company/owner-actions?state=open` lists them. Nothing in the queue sends, posts or
pays. A paid result is recorded in §4.

### Revenue plays (in the repo)

The levers that CAN live in the repo are supplied as O4 tickets, one per play
(`work_packet.REVENUE_PLAYS`, proof `probe:<metric>` + `play:<id>`): USDC pricing, a visible USDC
fee and seller's net, an A2A skill that quotes and accepts paid work, an SDK hire-and-pay example,
and a short dashboard path to a paid task. The play's files come first in the ticket's work packet.

### The daily report (`society/daily_report.py`)

`GET /v1/society/company/report`, and the first lines of the plans' `summary_text`: cycles in the
last 24h, model $ by role, builds delivered vs attempted, READY candidates, design reviews, the
north star and KRs against pace, and the open owner actions (one line each).

Nothing leaves AgentNet without the owner's approval.
