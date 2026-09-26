# Autonomous company mode — the live proof on staging

Company mode (docs/AUTONOMOUS_COMPANY.md, ADR-0009 D15) runs on the ONE
Society control plane. It adds a cadence and a portfolio discipline, not
authority. This is the record of it running on staging with the **live**
model. It is written from the proof's own output.

**Result (2026-09-26): the live Society settled two company cycles, the operator cycle and the day's scheduled cycle. GREEN 9/9 each. One Scout run was dead-lettered on invalid model JSON (§3).**
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

## 2. The cycles — GREEN 9/9 each

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

### The scheduled cycle

The worker opened today's **scheduled** cycle `e85b7c68` itself at 01:32:40Z,
the moment `SOCIETY_COMPANY_CYCLE_ENABLED` reached it (the cycle hour is 01
UTC). Validator deployments `5950d853` and `c9e8b995` followed it
(`A2A_PROOF_CYCLE_ID=scheduled`, then `e85b7c68`):

| Check | Result |
| --- | --- |
| C05 | exactly one scheduled cycle for 2026-09-26: `e85b7c68=no_high_value_change` |
| C03 | settled: `no_high_value_change`, `{"runs": 2, "intents_executed": {"WRITE_MEMORY": 1}}` |
| C04 | correlation `ca3f52bb`: `Society_Governor:company.cycle:completed` on `openai_compatible/deepseek-flash`; `Society_Scout:company.cycle:dead` |
| C04 detail | Scout `dead`, attempt 3: `invalid structured output: decision is not valid JSON: Expecting ',' delimiter: line 1 column 1737` |
| C06 | 3 events; `WRITE_MEMORY:allow:executed` |

`A2A PROOF RESULT: GREEN (9 checks)` both times.

**Reading the outcome.** The Governor (product and strategy) and the Scout
(research, customer insight and growth) each observed the aggregate evidence
bundle and recorded a memory. Neither proposed a change, so the cycle settled
as `no_high_value_change`. The cycle's instructions name that as a valid
outcome: *"do not create work to look busy."* Nothing was scripted. C04
refuses any decision made by a provider other than the live one, and every
decision in both cycles came from it. (NO FAKE AUTONOMY: `ScriptedRoleModel`
and `FakeModel` output can never pass that check.)

**Not observed live:** the Society choosing to call an external A2A agent.
With the client enabled, the model did not propose `DISCOVER_A2A_AGENT` or
`REQUEST_A2A_TASK` in this window, and none was induced. That path (policy,
budgets, circuit breaker, approval gate, federation pump,
`a2a.task.finished`) is proven by `tests/society/test_a2a_society_company.py`.
The pump's outbound leg is the same client §3 of docs/A2A_LIVE_PROOF.md
exercised live.

## 3. A live failure: the Scout's invalid JSON

In the scheduled cycle, the live model answered the Scout with malformed
JSON three times: `Expecting ',' delimiter` at character 1736. It was not
truncation. `decide()` reports `finish_reason=length` separately, and
the cap is 4,000 output tokens. The run was retried with backoff, then
dead-lettered (`fail_run`). No intent from it was executed, and the cycle
still settled from the Governor's run. This is the designed failure path
(strict structured output, bounded attempts), and it is recorded, not hidden.

Two follow-ups are tracked outside this release:
* the worker records `model_provider` only on a successful decision, so a
  failed run reads `None/None` in the operator API even though it called the
  model;
* why the company-cycle context produces invalid JSON for the Scout.

The operator cycle's Scout, on the same model, completed.

## 4. The first attempt, and why it failed

Deployment `ae3eebd0` opened the cycle, then gave up after 20 minutes.
Result: `RED C03`. This was not a product fault. `company.SETTLE_AFTER` is 30
minutes, so a cycle is settled only once it is that old **and** every run on
its correlation has finished. A 20-minute poll could never pass. The proof now:

* defaults to 45 minutes;
* can follow a running cycle (`A2A_PROOF_CYCLE_ID`, or `scheduled` for
  today's scheduled cycle) instead of opening another;
* shows the cycle's runs, models and intents (C04, C06).

The follow-up runs against today's **scheduled** cycle are recorded in §2.

## 5. What company mode can never do

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
