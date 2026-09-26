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
