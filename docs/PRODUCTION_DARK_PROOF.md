# Production — the dark bring-up, and what was actually proven

The durable record of AgentNet's first production environment. Written from
live platform reads, not from intent. Where something is an owner action or an
unproven claim, it says so rather than rounding up.

**As of 2026-09-21T01:16Z — all six services SUCCESS.** Railway project `AgentNet`
(`4a40abc4-f650-406d-be04-3d3c27ffc7b1`), environment `production`
(`5e23ccb2-4ba1-417e-a1b4-5bf362d0d2c4`).

## 1. What production runs

Branch `production` at `2adda7094a9c1b7da60027e6f47f4141c7798cfd`, tree
`0bd98264a279cb2de47a534b9b4f2158d0ca3f10` — verified identical to the approved
`main` target and to the release branch the gate produced. See
docs/PRODUCTION_RELEASE.md for the one-time bootstrap and why every gate still
ran.

Production does **not** follow `main`. `main` is autonomously merged by the
Society; production deploys a branch the Society cannot write (ADR-0008 D8).

| Service | id | Source | Healthcheck | First deployment |
| --- | --- | --- | --- | --- |
| `prod-postgres` | `c35d9aa4` | `ghcr.io/railwayapp-templates/postgres-ssl:18` | — | `037a330a` SUCCESS |
| `prod-redis` | `e41f9636` | `redis:8.2` | — | `a1cd9245` SUCCESS |
| `prod-registry` | `055f188d` | `production` @ `/services/registry` | `/readyz` 300s | `5ef7d10a` SUCCESS |
| `prod-payment` | `0208f0ea` | `production` @ `/services/payment` | `/readyz` 300s | `80dd8d66` SUCCESS |
| `prod-worker` | `2dd99b33` | `production` @ `/services/worker` | `/metrics` 120s | `42f2afba` SUCCESS |
| `prod-dashboard` | `05bf5f71` | `production` @ `/services/dashboard` | `/healthz` 120s | `f2339d74` SUCCESS |

Two volumes, both production-only: `prod-postgres-volume` (`a7d05f88`) and
`prod-redis-volume` (`8429d02b`). There is no Society workspace volume,
because there is no Society.

The registry owns the schema through a pre-deploy command that runs in a
separate container and must exit non-zero to block the release (ADR-0008 D4).
Its live evidence: `registry: alembic already stamped — running upgrade`, then
`Application startup complete`, then `Uvicorn running on http://0.0.0.0:8000`.

## 2. Isolation from staging — structural, not asserted

Every production service id is disjoint from its staging counterpart
(`prod-registry` `055f188d` vs staging `registry` `ec110f06`, and so on for all
four). `describe-environment` on staging does not list any `prod-*` service,
and on production does not list any staging service. Each Railway environment
has its own private network, so the two cannot reach each other over internal
DNS (ADR-0008 D2).

Production shares **no secret** with staging. `JWT_SECRET_KEY`,
`FLASK_SECRET_KEY` and `INTERNAL_WORKER_TOKEN` were generated fresh in the
production environment with `${{secret(48)}}`; the datastore passwords with
`${{secret(32)}}`. Nothing was copied. A shared JWT secret would have made a
staging token valid in production (ADR-0008 D9).

**No secret value was ever retrieved, printed or compared.** That is structural
rather than a matter of discipline: the Railway API returns
`valuesRedacted: true` and never serves the values at all.

## 3. Network exposure — dark means dark

| Service | Public domains | TCP proxies |
| --- | --- | --- |
| all six | **0** | **0** |

Nothing in production is reachable from the internet. Services talk to each
other over `*.railway.internal` only. No DNS record was created or changed, and
`agentnet.io.vn` is untouched.

Consequence recorded honestly: `CORS_ALLOWED_ORIGINS` must be non-empty (the
code refuses empty, so that no host can accidentally ship allow-all), and no
legitimate public browser origin exists while production is dark. It is
therefore pinned to the dashboard's **private** origin — an origin no browser
can resolve, which grants nothing. **This must be set to the real origin before
any public exposure.**

## 4. Credential-name audit (names only, never values)

None of the forbidden names appears on any of the six services:

`SOCIETY_MODEL_API_KEY`, `LLM_API_KEY`, `SOCIETY_GITHUB_APP_PRIVATE_KEY_PEM`,
`SOCIETY_GITHUB_APP_PRIVATE_KEY_FILE`, `SOCIETY_GITHUB_TOKEN`,
`DEEPSEEK_API_KEY`.

Production holds no model credential and no GitHub App key. It cannot call a
model and it cannot push to GitHub, because the material to do either is not
present under any name.

## 5. Society absence

There is **no `society-worker` service in production**, and no workspace
volume for one. `prod-payment`, `prod-worker` and `prod-dashboard` carry no
`SOCIETY_*` variable at all. `prod-registry` carries the switches and every one
is off: runtime, autonomous code, auto-merge and staging-deploy all `false`;
promotion, GitHub-credential and deployment providers all `disabled`; model
provider `scripted`; `SOCIETY_OPERATOR_BOOTSTRAP_EMAILS` empty, so no
privileged identity is bootstrapped here.

This agrees with the code rather than substituting for it:
`validate_settings` raises `SocietyConfigError` from `__post_init__` when
`ENVIRONMENT=production` and any of those flags is true, so a production host
configured otherwise fails at startup instead of running.
`settings.production_deploy_enabled` remains a hard `False`.

**Least privilege, checked not assumed:** `prod-worker` has neither
`INTERNAL_WORKER_TOKEN` nor a payment URL. It reaches PostgreSQL, Redis and the
registry, and nothing else. That secret stays on `prod-payment`, which is the
only service that needs it.

## 6. Owner actions still outstanding

Neither is settable from an engineering session; both are recorded rather than
claimed as done.

1. **Apply `deploy/github/production-ruleset.json`** to protect the
   `production` branch. Until then the branch has no ruleset. The command and
   the two deliberate differences from `main` are in `deploy/github/README.md`.
2. **Enable Wait for CI** on the four application services. Verified off:
   `checkSuites: false`. The `on: push` trigger for `production` that makes the
   gate meaningful is already in `.github/workflows/ci.yml` (CI run 167 ran on
   the branch), so this is a single dashboard toggle per service.

## 7. What is NOT proven here

- **No HTTP smoke test was run against production from this session.** The
  engineering environment's network policy denies outbound to
  `*.up.railway.app` (403 on CONNECT), and production has no public domain in
  any case. Health is evidenced by Railway healthchecks gating each release and
  by service logs, not by an external probe. `deploy/production/validate.py`
  exists for an operator or an in-environment runner to execute.
- **Registration through the delivery path was not exercised live on staging.**
  The staging validator's `ensure_user` hit an existing account (HTTP 400),
  which short-circuits before any email logic. That path is covered by unit
  tests, not by a live run.
- Healthchecks gate a release; they are **not** continuous monitoring
  (ADR-0008 D5).
- **The two consecutive production validations (§48) did NOT run.** The intended
  method was a temporary in-environment `prod-validator` service, mirroring the
  `staging-validator` pattern, running `deploy/production/validate.py` against
  the private registry and dashboard origins. Creating that service was refused
  by this session's permission layer. Two alternatives were considered and
  rejected rather than attempted: running it from the *staging* validator would
  have had staging reach into production, breaking the very isolation this
  phase establishes; repurposing a live production service's start command
  would have taken that service down. The validator is written, tested and
  committed; it has not been executed against production.

  What this leaves unproven, specifically: the core-money-path smoke (C01-C03,
  including that registration answers 503 while delivery is disabled), the
  red-team probes, and the HTTP-level health matrix. What remains proven
  independently of it: every service's healthcheck is a real HTTP GET performed
  by the platform, and a deployment only reaches SUCCESS on a 2xx -- so
  `/readyz`, `/healthz` and `/metrics` each answered correctly at least once,
  from outside the container, for every service in the table above.

## 8. The finding this bring-up surfaced

Production Redis served with **no password** for a window during bring-up —
reachable by anything on the production private network, never from the
internet. Full account, including the two wrong diagnoses I published before
finding the real one, is ADR-0008 D11. It now fails closed: an empty
`REDIS_PASSWORD` refuses to boot rather than silently starting an open Redis,
and the runtime state is checkable from a log marker
(`redis: requirepass will be set (length 32)`, deployment `a1cd9245`) instead
of inferred from configuration.

## 9. PRODUCTION DARK SOAK START

**2026-09-21T01:16Z.** All six services reporting SUCCESS on their first
deployment from the `production` branch at `2adda709`. The environment is
deployed, private, and serving nobody by design.

The soak measures whether a production environment nobody is using stays
healthy: restart loops, memory growth, the worker's 30s poll cycle running
without error against an empty task table, and Postgres/Redis persistence
across the volumes. It does not measure load, because there is none and there
is no public surface to generate any.

Known-good deployment ids, recorded because Railway rollback restores an image
**and** its variables but only within the plan's image-retention window
(Hobby: 72h — ADR-0008 D6), after which rolling back to these specific
deployments is no longer possible:

```
prod-postgres   037a330a-1c46-4f28-ba96-dae295145a5a
prod-redis      a1cd9245-3073-4e44-8756-a3de35cd6460
prod-registry   5ef7d10a-4e29-4ac0-bc1c-43fa9b1a005d
prod-payment    80dd8d66-9c5e-49ac-9878-cf3a511cad97
prod-worker     42f2afba-8e41-4297-a00f-bdea273fe17a
prod-dashboard  f2339d74-0f07-4d28-84ab-d8396da021e4
```

Code rollback is not schema rollback (ADR-0008 D7): reverting the registry to
an earlier image does not undo a migration that already ran.
