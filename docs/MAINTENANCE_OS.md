# AgentNet Autonomous Maintenance OS

Design: [ADR-0010](adr/0010-autonomous-maintenance-os.md). State machine: [MAINTENANCE_STATE_MACHINE.md](MAINTENANCE_STATE_MACHINE.md).
Policy: [MAINTENANCE_POLICY.md](MAINTENANCE_POLICY.md). Release: [MAINTENANCE_RELEASE.md](MAINTENANCE_RELEASE.md).
Observability: [MAINTENANCE_OBSERVABILITY.md](MAINTENANCE_OBSERVABILITY.md). Proof status: [MAINTENANCE_LIVE_PROOF.md](MAINTENANCE_LIVE_PROOF.md).

## What it is

A control plane that keeps the public product in its desired state. Every trusted observation of a
violated contract becomes a **Maintenance Incident**; every incident gets at most one **Repair Case**;
every case reaches a terminal outcome — `AUTO_REPAIRED`, `AUTO_ROLLED_BACK`, `SAFELY_ESCALATED`,
`CANNOT_REPRODUCE`, `DUPLICATE_RESOLVED` or `POLICY_REFUSED` — and nothing waits forever.

```
public probes (HTTP, browser) ─┐
watchdog / CI / DB invariants ─┼─► maintenance_observations ─► MaintenanceIncident (fingerprint)
                               ┘                                   │  (coverage = active case, never memory)
                                                                   ▼
                     Maintenance Kernel (staging society-worker)  RepairCase ── reconcile every cycle
                       deterministic: triage, queue, budgets,        │
                       deadlines, retries, rescope, risk             ├─ activities (model = cognitive worker):
                                                                     │    Diagnose → Design (plan r1..N) → AuthorPatch
                                                                     │    → deterministic QA → Review + Security
                                                                     ├─ READY CodeCandidate → UNCHANGED promotion
                                                                     │    controller → main (GREEN auto-merge / owner merge)
                                                                     ▼
                     Release Controller (release-control, no model)  MaintenanceRelease (signed attestation)
                       recompute everything → preview → release/prod-<sha> PR → production CI → merge
                       → deploy changed services at the exact SHA → N healthy public observations
                       → SUCCEEDED   |   regression → rollback to known-good → reconciliation PR + freeze
                                                                     ▼
                     the incident's own monitor shows a healthy streak → AUTO_REPAIRED
```

## Where things run

| Component | Process | Holds |
|---|---|---|
| Maintenance Kernel (`app/maintenance/reconciler.py`) | the staging `society-worker` (`SocietyWorker.reconcile_maintenance`, in a thread each loop) | the Society's model credential (unchanged boundary), the isolated worktrees |
| Surface ingestion (`surface_ingest.py`) | the same monitor that already probes production | nothing |
| Release Controller + kernel watchdog (`release.py`, `watchdog.py`) | a separate `release-control` service: `python -m app.maintenance.release_worker` | the Maintenance Release GitHub App key file, a production-scoped Railway token, the attestation key. **Never** a model key (it refuses to start) |
| Deep-tier browser probe (`browser.py`, `deploy/maintenance/browser_probe.py`) | a scheduled job with a browser (CI runner or a probe service) | an event-producer user JWT for the structural ingress |
| Operator surface (`/v1/maintenance/*`) | the registry API | nothing new |

The production Society stays **OFF**. Production maintenance is performed by the deterministic
Release Controller; the Society only ever reaches `main`.

## Owner runbook (no Claude, no command-line archaeology)

**See everything.** Open `https://api.agentnet.io.vn/v1/maintenance/console`, paste an operator access
token (the same operator role as the Society operator API). It shows switches, the nothing-stranded
count, error budgets, cases awaiting you, active cases with next action and deadline, open incidents,
releases, freezes, heartbeats, KPIs and toil. The public, sanitized view is `/v1/maintenance/summary`.

**Approve an AMBER repair.** The escalation arrives as a GitHub PR (and an operator notification).
Its description and the case's escalation package (console → case) hold root cause, changes, tests,
QA, Security, risk reason, staging evidence and rollback plan. **Merging the PR is the approval**; the
kernel resumes the persisted case and the Release Controller releases it (it verifies on GitHub that
the merger is in `MAINTENANCE_RELEASE_OWNER_LOGINS`). To decline: close the PR and
`POST /v1/maintenance/cases/{id}/refuse`.

**RED / constitutional.** A RED repair is prepared and escalated with a PR; merging it never
releases it automatically — release it through the existing gate (`docs/PRODUCTION_RUNBOOK.md`).
Constitutional scope is refused outright.

**Resume a paused switch.** If `MAINTENANCE_GREEN_PROMOTION_ENABLED` or `MAINTENANCE_GREEN_RELEASE_ENABLED`
was off, the case escalates as resumable; turn the switch on and `POST /v1/maintenance/cases/{id}/resume`.

**Freeze production maintenance releases.** `POST /v1/maintenance/release-freezes {"reason": "..."}`;
lift with `POST /v1/maintenance/release-freezes/{id}/lift`. A failed rollback opens an owner-only
freeze automatically.

**Kill maintenance automation.** Set `MAINTENANCE_AUTONOMY_ENABLED=false` on the staging
society-worker (and on release-control). New repairs and releases stop; in-flight cases that are not
mid-release are handed to you as `SAFELY_ESCALATED(autonomy_disabled)`; a release already past its
merge is still carried to success or rollback. The public product is untouched.

**Inspect a rollback.** Console → recent releases → `rollback` (reason, per-service method and
target, result, reconciliation PR). A `rollback_parity` freeze lifts itself when the production branch
tree equals the restored known-good tree.

**Recover the controller.** Railway restarts crashed processes (`ALWAYS`). A kernel that stays down
raises `CONTROL_PLANE` incidents from the release-control watchdog (`kernel:liveness`); cases resume
where they stopped (downtime does not count against their deadlines).

**Revoke credentials in an emergency.** Maintenance Release GitHub App: GitHub → Settings →
Developer settings → the App → revoke/suspend the installation or delete the key (the public product
keeps running; releases fail closed). Railway release token: Railway → project → Settings → Tokens →
delete (same effect). The Society's credentials are separate and unaffected.

## What remains human (by design)

AMBER and RED approval, constitutional changes, billing/spend, new external credentials or providers,
legal/business decisions, destructive data actions, DNS/Cloudflare, lifting incident freezes (ADR-0009
law), and lifting an owner-only release freeze. Escalations go to the owner, never to an external
coding agent.
