# Autonomous company mode — the live proof on staging

Company mode (docs/AUTONOMOUS_COMPANY.md, ADR-0009 D15) runs on the ONE
Society control plane. It adds a cadence and a portfolio discipline, not
authority. This is the record of it running on staging with the **live**
model. It is written from the proof's own output.

**Result (2026-09-26): the live Society settled a company cycle. GREEN 9/9.**
Production has no Society (`SOCIETY_RUNTIME_ENABLED` is refused there), so company mode is
staging-only by design.

## 1. Configuration during the proof

On the staging `registry` and `society-worker` (set at the service for the
proof window, like `SOCIETY_RUNTIME_ENABLED`; `.railway/railway.ts` keeps them
`false` in IaC):

* `SOCIETY_COMPANY_CYCLE_ENABLED=true`: one scheduled cycle per UTC date from
  `SOCIETY_COMPANY_CYCLE_HOUR_UTC` (default 01), plus operator cycles.
* `A2A_SOCIETY_CLIENT_ENABLED=true`.
* `A2A_SOCIETY_DISCOVERY_ALLOWED_HOSTS` names only the disposable reference peer.
* Unchanged:
  * `SOCIETY_AUTO_MERGE_ENABLED=false`;
  * production deploy hard `False`;
  * the Governor's `REQUEST_A2A_TASK` stays approval-gated.

The operator status (`GET /v1/society/company`) reported
`a2a_server_enabled`, `a2a_federation_enabled` and
`a2a_society_client_enabled` true, `auto_merge_enabled` false and
`production_deploy_enabled` false.

## 2. The cycle — GREEN 9/9

Operator cycle `be57ed1f` was opened at 01:36:58Z by validator deployment
`ae3eebd0` and followed to settlement by deployment `d9bdac48`
(02:05:42–02:07:23Z) with `A2A_PROOF_CYCLE_ID=be57ed1f`.

| Check | Result |
| --- | --- |
| P10 | staging operator login |
| C01 | operator company status → 200, with the mode flags above |
| C05 | scheduled cadence: exactly 1 scheduled cycle for 2026-09-26 (at most one per UTC date) |
| C02 | the existing cycle `be57ed1f` found |
| C03 | settled: `no_high_value_change`, `{"runs": 2, "intents_executed": {"WRITE_MEMORY": 2}}` |
| C04 | correlation `e3f12fd7`: 2 role runs, both on the **live** provider `openai_compatible/deepseek-flash`: `Society_Governor:company.cycle:completed`, `Society_Scout:company.cycle:completed` |
| C06 | the cycle's story → 200: 3 events; intents `WRITE_MEMORY:allow:executed` ×2 |

`A2A PROOF RESULT: GREEN (9 checks)`, exit 0.

**Reading the outcome.** The Governor (product and strategy) and the Scout
(research, customer insight and growth) each observed the aggregate evidence
bundle and recorded a memory. Neither proposed a change, so the cycle settled
as `no_high_value_change`. The cycle's instructions name that as a valid
outcome: *"do not create work to look busy."* Nothing was scripted. C04
refuses any run whose provider is not the live one, and every run that
reached cognition was the live one. (NO FAKE AUTONOMY: `ScriptedRoleModel`
and `FakeModel` output can never pass that check.)

**Not observed live:** the Society choosing to call an external A2A agent.
With the client enabled, the model did not propose `DISCOVER_A2A_AGENT` or
`REQUEST_A2A_TASK` in this window, and none was induced. That path (policy,
budgets, circuit breaker, approval gate, federation pump,
`a2a.task.finished`) is proven by `tests/society/test_a2a_society_company.py`.
The pump's outbound leg is the same client §3 of docs/A2A_LIVE_PROOF.md
exercised live.

## 3. The first attempt, and why it failed

Deployment `ae3eebd0` opened the cycle, then gave up after 20 minutes.
Result: `RED C03`. This was not a product fault. `company.SETTLE_AFTER` is 30
minutes, so a cycle is settled only once it is that old **and** every run on
its correlation has finished. A 20-minute poll could never pass. The proof now:

* defaults to 45 minutes;
* can follow a running cycle (`A2A_PROOF_CYCLE_ID`, or `scheduled` for
  today's scheduled cycle) instead of opening another;
* shows the cycle's runs, models and intents (C04, C06).

A follow-up run against today's **scheduled** cycle (`A2A_PROOF_CYCLE_ID=scheduled`,
marker `phase8-company-3`) was started. Its result was not read back before
this session's permission boundary stopped further platform reads, so it is
**not claimed here**.

## 4. What company mode can never do

These hold in code and are pinned by tests, not by this proof:

* No second loop.
* No new agents and no new authority.
* No quota of PRs, commits or features.
* Portfolio caps: `SOCIETY_COMPANY_MAX_ACTIVE_HYPOTHESES` and
  `SOCIETY_COMPANY_MAX_HIGH_RISK_INVESTIGATIONS`.
* An open incident freezes autonomous merge authority
  (`incident_freeze_open(n)`), and only an operator lifts it. The live drill
  is I01–I03 in docs/A2A_LIVE_PROOF.md §3.
* `SOCIETY_RUNTIME_ENABLED=false` stops cycles along with everything else.
