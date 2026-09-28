# Maintenance Release Controller

Code: `app/maintenance/release.py` (controller), `release_providers.py` (the only credential readers),
`attestation.py`, `release_worker.py` (process), `watchdog.py`. Model-free. Trusted base (RED).

## Authority model

| Party | Production authority |
|---|---|
| Society (staging society-worker) | none: no production credential; reaches `main` only |
| Maintenance Release Controller (`release-control`) | narrow: merge an eligible `release/prod-<sha>` PR through the normal production ruleset; deploy/roll back production services by exact SHA / known-good deployment id — GREEN maintenance (and AMBER after a verified owner merge) only |
| Owner | AMBER/RED approval, constitutional authority, freezes, credentials |

## Provisioning release-control (owner steps; nothing here is automatic)

1. **GitHub App "AgentNet Maintenance Release"** (dedicated, not the Society App). Repository
   permissions: Contents read & write, Pull requests read & write, Checks read, Metadata read.
   No Administration, no Secrets, no Workflows, no Environments. Install on `vansyson1308/agentnet`
   only. Do **not** add it as a ruleset bypass actor. Download the private key.
2. **Railway**: create service `release-control` from this repository (registry image) with start
   command `python -m app.maintenance.release_worker`, no public domain, restart policy ALWAYS. Mount
   the App key as a file; set `MAINTENANCE_RELEASE_GITHUB_APP_ID`, `..._INSTALLATION_ID`,
   `..._PRIVATE_KEY_FILE`. Create a **production-environment** project token and set
   `MAINTENANCE_RAILWAY_TOKEN` (+ `MAINTENANCE_RAILWAY_AUTH_MODE=project`), `MAINTENANCE_RAILWAY_PROJECT_ID`,
   `MAINTENANCE_RAILWAY_ENVIRONMENT_ID` (production) and `MAINTENANCE_RAILWAY_SERVICE_IDS` (JSON of the
   six production service ids). Point `DATABASE_URL`/`POSTGRES_*` at the staging control-plane database
   (where the kernel writes releases). Set the same `MAINTENANCE_ATTESTATION_KEY` as the society-worker.
   Never set a model key or a Society GitHub credential here (the process refuses to start).
3. **Release preview** (`LivePreview`, `release_providers.py`): exact-SHA parity on an isolated,
   non-production environment (staging by default: its own database and staging-safe credentials).
   Set `MAINTENANCE_RELEASE_PREVIEW=staging_parity`, `MAINTENANCE_PREVIEW_RAILWAY_ENVIRONMENT_ID`,
   `MAINTENANCE_PREVIEW_RAILWAY_SERVICE_IDS`, `MAINTENANCE_PREVIEW_UI_ORIGIN`,
   `MAINTENANCE_PREVIEW_API_ORIGIN`, and a token scoped to that environment only as
   `MAINTENANCE_PREVIEW_RAILWAY_TOKEN`. The preview passes only when every service the release
   changes has the candidate SHA as its active deployment there, and the preview answers readiness,
   the monitored public-surface contract, the A2A card, no public `/metrics` and no wildcard CORS for
   a foreign origin. It is `pending` while that SHA builds and refuses the production environment.
   Without a complete configuration the controller refuses every release (fail closed).
4. Flip `MAINTENANCE_RELEASE_PROVIDER=live`, `MAINTENANCE_GREEN_RELEASE_ENABLED=true`,
   `MAINTENANCE_AUTONOMY_ENABLED=true` on release-control, and list `MAINTENANCE_RELEASE_OWNER_LOGINS`.

## Release flow

`pending` → **verify** (attestation digest/signature; the case is releasing this row; exact SHA on
`main`; `production` is an ancestor; production..sha diff == the attested changed files and not
truncated; trusted class GREEN or verified-owner AMBER; required checks passed on the SHA; Railway
schema offers `serviceInstanceDeployV2(commitSha)` and `deploymentRollback`; provider healthy; no
release freeze; no foreign incident freeze; error budget; daily cap) → record **known-good** (current
deployment id per service, production sha, tree) and a **baseline probe** → `preview_validating` →
branch `release/prod-<sha12>` at the exact SHA → PR to `production` → `pr_open` (refused on conflict,
failed CI, a moved production branch or a changed head; waits on pending CI) → merge with the expected
head → `deploying` (only the changed services; adopt a Wait-for-CI deploy of that exact commit, else
trigger one after a grace period, recorded before anything else) → `post_deploy_verifying` (N healthy
observations; a regression is an item healthy in the baseline and failing now) → `succeeded` + new
known-good record.

Transient provider failures back off (bounded count and deadline) and then **refuse**; after the merge
they lead to rollback, never to a second deploy.

## Rollback

Per changed service: `deploymentRollback(known_good_deployment_id)`; if refused (image expired,
`canRollback=false`), redeploy the known-good SHA. Success requires the baseline-healthy public items
healthy again for N observations. Then a **reconciliation PR** (`reconcile/prod-<id>`: one commit whose
tree is the known-good tree, parent = current production head) and a `rollback_parity` release freeze
that the controller lifts only when the production branch tree equals the known-good tree (it merges
the reconciliation PR itself once its CI passes — it only restores known-good content). A failed
rollback → `rollback_failed`, owner-only P0 freeze, case `SAFELY_ESCALATED`, no retries.

Never: auto-downgrading a production database (a release with a migration is never GREEN), deploying
"latest main", deploying into an unhealthy provider, retrying a production deploy in a loop.

## Proving rollback without production chaos

Controlled failures run against fakes in CI (`tests/society/maintenance/test_kernel_e2e.py`,
`test_kernel_scenarios.py`) and — for the live provider proof — against a preview/staging service,
never production. Production rollback capability is verified non-destructively (schema discovery,
`canRollback` on the recorded known-good deployments).
