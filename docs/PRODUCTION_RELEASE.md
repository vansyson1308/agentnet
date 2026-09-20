# Production release — the trusted boundary

AgentNet Society autonomously improves **staging** (`main`). It has no path to
production. This document is the release flow that keeps that true.

## The shape

```
        AGENTNET SOCIETY (staging)
                 │  autonomous GREEN merge
                 ▼
               main ──────────────► staging Railway (auto-deploys)
                 │
                 │  explicit, operator-chosen target SHA
                 ▼
     deploy/production/release.py   ← read-only preflight, fails closed
                 │
                 ▼
        release/prod-<shortsha>
                 │  PR
                 ▼
            production  (protected branch, Society is not a bypass actor)
                 │  Railway Wait for CI
                 ▼
        PRODUCTION Railway environment
```

**Production does not follow `main`.** If it did, staging evaluation would be
decorative and the Society would hold production authority through a branch it
can write (ADR-0008 D8).

## Running the gate

```bash
# READ-ONLY. This is the default and it is the normal thing to run.
python deploy/production/release.py \
  --target <APPROVED_MAIN_SHA> \
  --staging-sha registry=<SHA> --staging-sha payment=<SHA> \
  --staging-sha worker=<SHA>   --staging-sha dashboard=<SHA> \
  --current-release <SHA production runs today>

# Only after the preflight passes and a human has read it:
python deploy/production/release.py --target <APPROVED_MAIN_SHA> ... --execute
```

`--execute` creates `release/prod-<shortsha>` and verifies its tree equals the
approved target's tree. It does **not** push, open the PR, or merge. A trusted
operator does those, deliberately.

## What the gate checks, and why each one exists

| Gate | Refuses | Because |
| --- | --- | --- |
| `target_shape` | `latest`, short strings | production always has an explicit target |
| `target_exists` | unknown SHAs | you cannot release what does not exist |
| `target_on_main` | commits off `main` | only the line staging tests is releasable |
| `target_ci_green` | red or pending CI | untested code never reaches production |
| `no_autonomous_merge_freeze` | releasing during a freeze | a freeze means something is already wrong |
| `staging_evidence:<service>` | a runtime subtree that changed and was never staged | see below |
| `no_sensitive_changes` | migrations, auth, payment, Society policy, credentials, CI, deployment foundation, release machinery | owner-reviewed release events (ADR-0008 D7) |
| tree equality | a release branch whose source differs from the approved target | the wrapper commit may differ; the source must not |

### Staging evidence is a subtree question, not a SHA question

Railway legitimately **skips** a deployment when a commit misses a service's
watch paths — a docs-only merge deploys nothing. So "did staging deploy exactly
this SHA?" is the wrong question and would make ordinary targets unreleasable.

The gate asks instead: **is this service's runtime subtree the same tree staging
last ran?** An unchanged subtree inherits the evidence of the SHA staging did
deploy. A subtree that *changed* and was never deployed is not releasable, which
is precisely the case that matters.

### Sensitive changes fail closed

A rollback restores an image. It does not un-migrate a database. So an ordinary
release must contain no migration; when one appears, the gate blocks and the
release becomes an explicit owner decision (`--allow-sensitive`, after a real
review). The acknowledgement is still **recorded** in the verdict — an
acknowledged sensitive release is not an invisible one.

## The Society cannot reach any of this

* No Society module imports `deploy.production` — asserted by
  `tests/test_production_release_gate.py`.
* There is no `DEPLOY_PRODUCTION` intent and no executor for one.
* `production_deploy_enabled` is a read-only `False` in Society settings, and a
  test pins it.
* The Society GitHub App is **not** a bypass actor on the `production` ruleset.
* `.railway/production.ts` refuses any environment but `production` and declares
  no Society service and no model credential.

## Rollback

Within the plan's image-retention window (Hobby: **72 hours**) a Railway
rollback restores the previous image **and its variables** without rebuilding —
so it also reverts a bad variable change. Rolling back to an *arbitrary* older
deployment is a dashboard action.

Outside the window there is no rollback; use redeploy (rebuilds from that
deployment's source) or release a Git revert through this same gate.

**Never downgrade a production database.** If the bad release contained a
migration, code rollback is not enough and the recovery is an owner decision.
