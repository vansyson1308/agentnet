# Maintenance Policy (trusted base)

Code: `app/maintenance/policy.py`, `slo.py`, `state_machine.py`, `config.py`. Policy **decides**;
enforcement happens where the action happens (kernel, promotion freeze check, release controller).
No intent, activity or model output can change a bound below.

## Risk classes

| Class | Examples | Outcome |
|---|---|---|
| `MAINTENANCE_GREEN` | templates, static CSS/JS, docs — base-GREEN paths, bounded diff, no sensitive semantics, no evaluation artefact, no reward hacking | may be merged to `main` autonomously (promotion controller, auto-merge on) and released by the Release Controller |
| `AMBER` | ordinary product code (e.g. dashboard route handlers), auth/session words in the diff, > 8 files or > 400 changed lines | prepared completely, PR opened, `SAFELY_ESCALATED`; owner merge = approval; then released automatically after the controller verifies the owner merge |
| `RED` | Society/kernel/policy/risk/fitness/promotion, auth core, payment/wallet/escrow/task_service, migrations/schema, deploy/CI/Docker/requirements, `.railway`, trusted evaluation (contracts, detectors, verification tests), money semantics in the diff, DNS/edge | prepared and escalated; never released automatically, even after the owner merges |
| `CONSTITUTIONAL` | NEVER-tier paths (secrets, keys, git internals), deleted/skipped tests, disabled gates, anti-reward-hacking findings | `POLICY_REFUSED` |

**Evaluation laundering** — product change + the contract/detector/test that judges it in one
change — is RED. **Anti-reward-hacking** (CONSTITUTIONAL): catch-all `except ...: pass/return`, adding
`url_build_error_handlers`, a `return '#'` link fallback, rewriting `errorhandler(404/500/Exception)`,
hiding `.alert/.flash` with `display:none`, removing a `flash(...)` error message, disabling a feature
flag. (Disabling a feature as the intended safe state is an owner decision, not a GREEN repair.)

A GREEN class is necessary, not sufficient: the Release Controller additionally requires CI on the
exact SHA, a passing release preview, no foreign incident freeze, no release freeze, error budget,
the daily cap and a healthy provider.

## Bounds (and why)

| Bound | Value | Why |
|---|---|---|
| plan revisions per case | 3 | the first plan + two evidence-driven rescopes; a fourth means the problem is not understood — escalate |
| repair attempts per case | 4 | each attempt already iterates up to 10 turns with 4 test runs; four fresh attempts cover QA and Security feedback twice |
| GREEN scope | ≤ 8 files, ≤ 400 changed lines | a presentation repair larger than that is a redesign (innovation lane) |
| case wall clock before `main` | 24 h | P2 work should not linger more than a day; release states have their own deadlines |
| model spend per case | $0.50 (P0/P1 $1.00) | observed live cost is ~$0.002 per run; this allows ~250 turns |
| maintenance model budget | $1.00/day with $0.40 reserved for P0/P1 | separate from the Society budget; polish can never starve incident response |
| activity tries | 3 | one transient failure and one retry of a real problem; a third failure is signal |
| queue | 1 urgent + 2 routine active cases | Builder/QA capacity on one staging worker; urgent may borrow a routine slot, never the reverse |
| autonomous production maintenance releases | 1/day | first autonomous releases; raise only by an owner change after evidence |
| post-deploy healthy observations | 3 × 60 s | enough to catch a crash loop or a broken page, short enough to finish before the next monitor cycle |
| recovery streak | 3 healthy observations | the monitor needs ≥ 2 failures to open; 3 successes to close is deliberately stricter |
| reopen cooldown / cases per incident | 24 h / 3 | a failed repair does not mean solved; it also must not loop hourly |
| stall bound | 15 min | one lease; anything overdue longer without a lease is a control-plane defect |

## Priority and lanes

Priority is derived: money/data invariants P0; critical availability P0; security P0/P1; auth and A2A
journeys at least P1; minor severity P3; otherwise the collector's base. Triage lanes that never
produce a patch: money/data invariants (freeze + P0 escalation; data repair stays owner-controlled),
external dependency, runtime unavailability (no code "fix" for an outage), contracts marked
non-autonomous (money, security, email, dependency posture), control-plane incidents (the kernel never
repairs itself).

## Error budget policy

With every availability-class SLO within budget: normal GREEN maintenance and normal innovation.
With one exhausted: innovation/feature promotion freezes (`innovation_freeze_reasons`), P2/P3 releases
wait; P0/P1 repairs, security repairs and rollbacks continue. An active P0 case also freezes innovation
merges. Budgets need a minimum sample count before they are judged.

## Incident freezes and the repair exception

Availability/security/money incidents open an operator-lifted incident freeze. Exactly one kind of
change passes it: the maintenance repair of the frozen incident (linked case in `PROMOTING`, changed
files inside its immutable plan, GREEN or AMBER class). Nothing else merges.
