# ADR-0008 — The production release boundary

Status: accepted (Phase 7)
Date: 2026-09-20
Supersedes nothing. Builds on ADR-0006 (Railway staging) and ADR-0004 (self-development).

## Context

AgentNet Society autonomously evolves `main` in staging and, since 2026-09-20, merges
GREEN changes there without a human (docs/SOCIETY_LIVE_PROOF.md §9). Production must
exist without inheriting that authority. This ADR records the **external contracts**
Phase 7 depends on, read from Railway's current documentation rather than assumed, and
the decisions that follow from them.

## D1 — Production is an EMPTY environment, never a duplicate of staging

Railway creates a `production` environment with every project by default; this project's
was never used and contains no services. Railway offers two creation modes:

* **Duplicate Environment** — "creates a copy of the selected environment, **including
  services, variables, and configuration**".
* **Empty Environment** — "creates an empty environment with no services".

Duplicating staging would copy the Society's flags and the *names and values* of its
variables. Staging holds a model credential and a GitHub App private key. Duplication is
therefore refused on principle, not merely avoided. We reuse the existing empty
`production` environment and create each resource explicitly.

Note a second, subtler reason: **sealed variables are not copied when duplicating an
environment**. A duplicate would silently produce a production service whose secrets are
*absent* rather than *fresh* — a failure that looks like success until runtime.

`Sync environments` is refused for the same reason: it imports services from another
environment.

## D2 — Isolation is a platform property, and we verify it structurally

"Each environment has its own isolated network." Services address each other over
internal DNS as `SERVICE_NAME.railway.internal`, scoped to the environment. "All changes
made to a service are scoped to a single environment."

We do not take this on faith. Phase 7 asserts, from live IDs: production Postgres service
id != staging Postgres service id, production Redis service id != staging Redis service
id, and the environment ids differ. New services are created in production rather than
the existing staging services being given a second environment configuration — the latter
would share a service id and make the assertion unprovable.

**Environment RBAC**, which would restrict who can read production variables and logs, is
an Enterprise feature. It is unavailable on this plan and is recorded as an owner
hardening option, not a Phase-7 gate.

## D3 — Wait for CI is a workflow-level gate, and it requires `on: push` for the branch

Railway's documented behaviour:

* Requires a workflow with an `on: push: branches:` directive for the branch.
* Deployments sit in `WAITING` until **every GitHub Actions check suite on the commit**
  has finished. Railway "looks at the conclusion of each **workflow run**, not at
  individual jobs. Checks from other GitHub apps are ignored."
* A **failed** workflow skips the deployment immediately.
* A **skipped** or **neutral** workflow never blocks.
* A **cancelled** workflow blocks only if no other workflow on the commit succeeded.
* If workflows have not finished after **two hours**, the deployment is skipped.

Two consequences Phase 7 acts on:

1. Adding `push: [production]` to `.github/workflows/ci.yml` is a **prerequisite** for
   Wait for CI on the production branch, not a nicety. Without it there is no check suite
   on a `production` push and the gate is vacuous.
2. Because the gate is per-workflow-run and ignores other apps, it is *not* a substitute
   for branch protection. Required status checks on the `production` ruleset are what
   actually prevent a bad commit from reaching the branch; Wait for CI prevents a bad
   commit that is already on the branch from being *deployed*. Phase 7 uses both.

Railway's own guidance: "If a workflow must run before every deploy, such as a database
migration, don't rely on Wait for CI alone… run the step as a pre-deploy command." That
is exactly D4.

## D4 — One migration owner, enforced by a pre-deploy command

Staging already gives the registry sole ownership of bootstrap + `alembic upgrade head`
through a Railway **pre-deploy command**, which runs in a separate container before the
new deployment starts and must exit non-zero on failure; the runtime container then starts
with `SKIP_DB_BOOTSTRAP=true`. Production repeats this exactly, minus the Society fleet
seed — production runs no Society, so seeding its roles would create rows the public
application never reads.

`payment`, `worker` and `dashboard` never migrate. This is what stops a multi-service
deploy from racing `alembic` against itself.

## D5 — Healthchecks gate the deployment, but do not monitor it

"If the deployment **has** a healthcheck configured, Railway will mark the deployment as
`Active` when the healthcheck succeeds." Default timeout 300s
(`RAILWAY_HEALTHCHECK_TIMEOUT_SEC`). Critically: "Railway does **not** monitor the
healthcheck endpoint after the deployment has gone live" — it is a release gate, not
uptime monitoring. Continuous monitoring remains an unsolved, externally-owned concern
and is recorded as such rather than implied.

Railway performs healthchecks from the hostname `healthcheck.railway.app`; an application
that restricts by Host must allow it. AgentNet does not filter by Host, so no change is
needed — but it is recorded because a future host-allowlist would silently break deploys.

Services with an attached volume incur brief downtime on redeploy even with a healthcheck,
because Railway refuses two active deployments mounting one volume. Production has **no
volumes** (the only volume in the project is the Society workspace, staging-only), so
production redeploys are zero-downtime.

## D6 — Rollback restores an image and its variables, and it expires

* **Rollback** and **Redeploy** both "use the source code from the selected deployment".
* Rolling back within the retention window "will restore the previous image, settings,
  and **all variables** with a new deployment; no redeployment is required" — so a
  rollback also reverts a bad *variable* change, not just bad code.
* Outside the window there is no rollback; only redeploy, which **rebuilds** from the
  original source with the deployment's original variables.
* **Image retention by plan**: Free/Trial 24h · **Hobby 72h** · Pro 120h · Enterprise 360h.
* "The dashboard is the only place to roll back to an arbitrary older deployment. The CLI
  covers the redeploy and restart cases."

Phase 7 therefore does **not** claim arbitrary rollback as an exercised capability from a
non-interactive session. It records a known-good deployment id per service, proves the
redeploy path, and documents the dashboard action and the Git revert fallback. Retention
is finite, which is precisely why the known-good id and the release tree are written down
rather than assumed recoverable.

## D7 — Code rollback is not schema rollback

A rollback restores an image. It does not un-migrate a database. An ordinary production
release must therefore contain **no migration**. When a release diff touches migrations,
`init-db`, or DB bootstrap, the release gate fails closed and the release becomes an
explicit owner-reviewed event. AgentNet never automatically downgrades a production
database.

## D8 — Production does not follow `main`

Staging deploys from `main`, which the Society merges to autonomously. If production also
followed `main`, staging evaluation would be decorative and the Society would hold de
facto production authority through a branch it can write.

Production services deploy from a dedicated **`production`** branch that the Society
cannot write: it is outside the autonomous branch prefix, the Society GitHub App is not a
bypass actor on its ruleset, and no Society intent targets it. Content reaches it only
through a release PR whose source tree equals an approved, staging-validated `main` SHA.

## D9 — Secrets are created in production, never copied into it

Fresh `JWT_SECRET_KEY`, `FLASK_SECRET_KEY` and `INTERNAL_WORKER_TOKEN` are generated for
production and never read back. Staging values are never reused: a shared JWT secret would
make a staging token valid in production.

**Sealing**: Railway can seal a variable so its value "is provided to builds and
deployments but is never visible in the UI nor can it be retrieved via the API". The
documented path is a per-variable UI action ("Seal" in the 3-dot menu) and sealed values
are deliberately unavailable to the CLI and API. No seal flag is exposed by the connector
available to this session, so Phase 7 sets the variables unsealed and records sealing as a
**recommended owner hardening action**, per the Phase-7 instruction not to block on it.
Sealing is one-way and cannot be undone.

The model credential and the GitHub App private key are **absent** from production, proven
by a variable-NAME audit. Values are never inspected.

## D10 — Cost is bounded by shape, not by a raised limit

Hobby includes $5 of usage/month; RAM is $10/GB/month and CPU $20/vCPU/month, billed on
measured use. Production adds six resources (Postgres, Redis, registry, payment, worker,
dashboard) at one replica each and no volume. No plan upgrade, billing-limit change or
add-on is made; if a usage limit objectively blocks production, that is reported as an
owner action rather than silently resolved by spending money.

## Consequences

* Production exists, is isolated, and holds no Society or model credential.
* `main` continues to evolve autonomously; production changes only by explicit release.
* A release is reproducible from three recorded facts: the approved `main` SHA, the
  production branch tree, and the Railway deployment ids.
* Recovery inside 72h is a rollback; outside it, a redeploy or a Git revert release.
* Continuous production monitoring and DNS cutover remain deliberately outside Phase 7.
