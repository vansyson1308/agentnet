# Maintenance State Machine (Repair Cases)

The executable source of truth is `services/registry/app/maintenance/state_machine.py`
(`TABLE`). The table below is **generated** from it by
`state_machine.markdown_table()`; `tests/society/maintenance/test_state_machine.py`
fails when the two disagree. Regenerate instead of editing by hand:

```bash
cd services/registry && python -c "from app.maintenance.state_machine import markdown_table; print(markdown_table())"
```

## Laws

* **Every non-terminal state has an exit.** A timeout (`deadline_at`), a poll
  cadence (`next_action_at`), a try budget and two recovery targets: the
  **deadline recovery** the watchdog executes when `now > deadline_at` and no
  lease is valid, and the **exhaustion target** when the state's tries are
  used up. `assert_total()` proves at import time that every recovery target
  is a legal watchdog move and that every state reaches a terminal state.
* **The database enforces liveness.** `repair_cases_liveness`: a non-terminal
  row always has `next_action_at` and `deadline_at`. The "nothing stranded"
  invariant is a constraint, not a hope.
* **Terminal means terminal.** `AUTO_REPAIRED`, `AUTO_ROLLED_BACK`,
  `CANNOT_REPRODUCE`, `DUPLICATE_RESOLVED` and `POLICY_REFUSED` have no exit.
  `SAFELY_ESCALATED` has exactly one kind of exit: an **owner** action on a
  *resumable* escalation (an approval request). The machine never
  un-escalates itself.
* **A model never moves a case past verification.** `activity` actors can
  only produce plans, patches, rescopes and escalations; they can never reach
  `READY_FOR_RELEASE`, `RELEASING`, `AUTO_REPAIRED`, `AUTO_ROLLED_BACK` or
  `CANNOT_REPRODUCE` (tested).
* **Facts beat timeouts.** A release outcome already persisted by the Release
  Controller decides a releasing case even when the kernel wakes after the
  deadline.
* **Controller downtime is not work time.** When the kernel heartbeat shows a
  gap longer than 5 minutes, live deadlines move by the gap on restart, so a
  restart continues cases instead of escalating them; a kernel that stays
  dead is caught by the separate watchdog (`CONTROL_PLANE` incident).
* **Audit.** Every transition appends `repair_transitions(from, to,
  actor_type, actor_id, reason_code, evidence_digest, created_at)`; an
  update of that table is refused by a trigger.

## Deadlock-impossibility assertion

```
COUNT(non-terminal repair_cases
      with no active lease
      and (next_action_at IS NULL or next_action_at < now - MAINTENANCE_STALL_SECONDS)
      and not awaiting explicit approval) = 0
```

`reconciler.stranded_count()` evaluates it every cycle; a non-zero count
opens a `CONTROL_PLANE` incident (`kernel:case_liveness`). "Awaiting explicit
approval" is the terminal-but-resumable `SAFELY_ESCALATED`, so it never counts.

## Transition table (generated)

| State | Purpose | Timeout | Poll | Max tries | Recovery | Allowed next states (actor) |
|---|---|---|---|---|---|---|
| `DETECTED` | confirm the violation from a fresh trusted observation | 30m | 1m | 40 | on deadline -> `CANNOT_REPRODUCE`; tries exhausted -> `CANNOT_REPRODUCE` | `CONFIRMED` (controller); `CANNOT_REPRODUCE` (controller, watchdog); `DUPLICATE_RESOLVED` (controller); `SAFELY_ESCALATED` (controller, owner, watchdog); `POLICY_REFUSED` (controller, owner) |
| `CONFIRMED` | deterministic triage lanes, then wait for maintenance capacity and budget (priority queue) | 24h | 5m | 400 | on deadline -> `SAFELY_ESCALATED`; tries exhausted -> `SAFELY_ESCALATED` | `TRIAGED` (controller); `CANNOT_REPRODUCE` (controller, watchdog); `SAFELY_ESCALATED` (controller, owner, watchdog); `POLICY_REFUSED` (controller, owner) |
| `TRIAGED` | a maintenance slot is held; schedule diagnosis | 10m | 0m | 3 | on deadline -> `SAFELY_ESCALATED`; tries exhausted -> `SAFELY_ESCALATED` | `DIAGNOSING` (controller); `CANNOT_REPRODUCE` (controller, watchdog); `SAFELY_ESCALATED` (controller, owner, watchdog); `POLICY_REFUSED` (controller, owner) |
| `DIAGNOSING` | DiagnoseIncident + DesignRepair activities produce immutable plan revision 1 | 30m | 0m | 8 | on deadline -> `SAFELY_ESCALATED`; tries exhausted -> `SAFELY_ESCALATED` | `PLAN_READY` (activity, controller); `CANNOT_REPRODUCE` (controller, watchdog); `SAFELY_ESCALATED` (activity, controller, owner, watchdog); `POLICY_REFUSED` (controller, owner) |
| `PLAN_READY` | trusted risk classification of the plan revision; refuse constitutional scope | 10m | 0m | 3 | on deadline -> `SAFELY_ESCALATED`; tries exhausted -> `SAFELY_ESCALATED` | `BUILDING` (controller); `NEEDS_RESCOPE` (controller); `CANNOT_REPRODUCE` (controller, watchdog); `SAFELY_ESCALATED` (controller, owner, watchdog); `POLICY_REFUSED` (controller, owner) |
| `BUILDING` | bounded RepairAttempt: read, patch, targeted test, feedback, patch | 1h | 0m | 4 | on deadline -> `BUILDING`; tries exhausted -> `NEEDS_RESCOPE` | `VERIFYING` (activity, controller); `BUILDING` (activity, controller, watchdog); `NEEDS_RESCOPE` (activity, controller, watchdog); `CANNOT_REPRODUCE` (controller, watchdog); `SAFELY_ESCALATED` (activity, controller, owner, watchdog); `POLICY_REFUSED` (controller, owner) |
| `VERIFYING` | independent QA (trusted tests), ReviewPatch, SecurityReview, trusted risk recompute | 45m | 0m | 8 | on deadline -> `SAFELY_ESCALATED`; tries exhausted -> `SAFELY_ESCALATED` | `PROMOTING` (controller); `BUILDING` (activity, controller); `NEEDS_RESCOPE` (activity, controller); `CANNOT_REPRODUCE` (controller, watchdog); `SAFELY_ESCALATED` (activity, controller, owner, watchdog); `POLICY_REFUSED` (controller, owner) |
| `NEEDS_RESCOPE` | DesignRepair with Builder/QA evidence proposes plan revision N+1 (never widens N) | 30m | 0m | 4 | on deadline -> `SAFELY_ESCALATED`; tries exhausted -> `SAFELY_ESCALATED` | `PLAN_READY` (activity, controller); `CANNOT_REPRODUCE` (controller, watchdog); `SAFELY_ESCALATED` (activity, controller, owner, watchdog); `POLICY_REFUSED` (controller, owner) |
| `PROMOTING` | the trusted promotion controller carries the verified patch to main (PR, CI, merge) | 6h | 2m | 400 | on deadline -> `SAFELY_ESCALATED`; tries exhausted -> `SAFELY_ESCALATED` | `READY_FOR_RELEASE` (controller); `BUILDING` (controller); `SAFELY_ESCALATED` (controller, owner, watchdog); `POLICY_REFUSED` (controller, owner) |
| `READY_FOR_RELEASE` | attest the merged repair and hand it to the Release Controller | 2h | 1m | 200 | on deadline -> `SAFELY_ESCALATED`; tries exhausted -> `SAFELY_ESCALATED` | `RELEASING` (controller); `SAFELY_ESCALATED` (controller, owner, watchdog); `POLICY_REFUSED` (controller, owner) |
| `RELEASING` | mirror the Release Controller (verify, preview, production PR, deploy) | 4h | 1m | 500 | on deadline -> `SAFELY_ESCALATED`; tries exhausted -> `SAFELY_ESCALATED` | `POST_RELEASE_VERIFYING` (controller, release); `RECOVERY_PENDING` (controller, release); `AUTO_ROLLED_BACK` (controller, release); `SAFELY_ESCALATED` (controller, owner, release, watchdog) |
| `POST_RELEASE_VERIFYING` | the incident's own monitor must show a healthy streak on the released product | 1h | 1m | 200 | on deadline -> `DIAGNOSING`; tries exhausted -> `SAFELY_ESCALATED` | `AUTO_REPAIRED` (controller); `RECOVERY_PENDING` (controller, release); `DIAGNOSING` (controller, watchdog); `SAFELY_ESCALATED` (controller, owner, watchdog) |
| `RECOVERY_PENDING` | the Release Controller restores known-good; public desired state must be healthy | 1h | 1m | 200 | on deadline -> `SAFELY_ESCALATED`; tries exhausted -> `SAFELY_ESCALATED` | `AUTO_ROLLED_BACK` (controller, release); `SAFELY_ESCALATED` (controller, owner, release, watchdog) |
| `SAFELY_ESCALATED` | complete review package delivered to the owner; no agent runs | - | - | - | owner resume only | `READY_FOR_RELEASE` (owner); `PROMOTING` (owner) |

## Terminal outcomes

| Outcome | Meaning |
|---|---|
| `AUTO_REPAIRED` | The repair was released, the release passed post-deploy verification, and the incident's own monitor showed a healthy streak on the released product. |
| `AUTO_ROLLED_BACK` | The release regressed the public desired state; the Release Controller restored the known-good deployments and the public contract is healthy again. |
| `SAFELY_ESCALATED` | A complete review package is with the owner. Resumable (approval request) or final. No agent runs while it waits. |
| `CANNOT_REPRODUCE` | The violation stopped before any change was merged (a trusted recovery streak) -- no random patch. |
| `DUPLICATE_RESOLVED` | Reserved for a case superseded by another case for the same fingerprint (the one-active-case index normally prevents this). |
| `POLICY_REFUSED` | Trusted policy refused the scope or the patch (constitutional path, NEVER finding, anti-reward-hacking finding). |
