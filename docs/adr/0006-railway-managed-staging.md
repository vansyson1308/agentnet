# ADR-0006 — Managed staging on Railway (Phase 4)

Status: accepted (2026-09-18) · Refines ADR-0003 (hosting-neutral contract) and ADR-0005 D3 (staging
contract). Bring-up: **DONE — `MANAGED STAGING — GREEN`** (D12; the D11 blocker was lifted the same day by the
Railway connector); repository side complete.

## Context

Phase 3.1 closed the pre-deploy boundaries; no host existed and "final managed hosting" was NOT
SELECTED. Phase 4 selects Railway as the managed staging platform and prepares the repository so the
staging environment can be created from validated `main` with one migration owner, private
networking, no custom DNS, no live model, no Society GitHub credential and every dangerous Society
flag OFF.

Constraints carried into the phase: no production environment; no DNS change (`agentnet.io.vn` and
`139.180.143.222` untouched); no DeepSeek key and no model call; no Society GitHub App secret and no
real promotion; no plan upgrade or paid add-on without explicit approval; no token, key or password
is ever requested from the user; the model never gains an executor.

Environment facts (2026-09-18): every Railway host — `railway.com`, `railway.app`,
`docs.railway.com`, `backboard.railway.com`, `cli.railway.com`, `mcp.railway.com`,
`*.up.railway.app` — answers `CONNECT 403` from this session's organisation egress policy; no Railway
MCP connector is enabled for the session; `github.com/login` is blocked and the GitHub `rulesets`
API path is refused by the session's GitHub proxy. Railway's official documentation was therefore
read from its published source, `github.com/railwayapp/docs` at `63cab08` (2026-09-17), through
the git proxy.

## Decisions

### D1 — Infrastructure as Code, not config-as-code

`.railway/railway.ts` (Railway IaC DSL, npm `railway` 3.11.0, module `railway/iac`) declares the
whole staging topology and is applied from a linked checkout with `railway config plan` /
`railway config apply`. `railway.toml` / `railway.json` config-as-code is deprecated (hard cutoff
2026-12-01; new services cannot opt in) and is not used. The file throws unless the linked
environment is `staging`, contains no secret value (shared-variable references only) and
type-checks against the published DSL (`npx tsc --noEmit --strict`). Settings the DSL cannot express
are dashboard/CLI steps in `docs/RAILWAY_STAGING.md`: generated public domains, Wait for CI, watch
paths, restart policy, the pre-deploy timeout and the "no Public Access" confirmation for the
databases.

### D2 — Topology

Project `AgentNet`, environment `staging` — never the default `production` environment. Services:
managed `postgres` and `redis` (private, no TCP proxy), `registry` (public), `payment` (private),
`worker` (private), `dashboard` (public), `society-worker` (private; the only service with a
volume). No `simulation` service; no Jaeger (`JAEGER_ENABLED=false`; spans still persist to
PostgreSQL). Root directories: `services/registry`, `services/payment`, `services/worker`,
`services/dashboard`; the society worker builds `services/registry` with a custom start command.
The existing Dockerfiles are used; the registry image additionally copies the bootstrap script.

### D3 — Exactly one migration owner

The registry's pre-deploy command `sh -c 'SKIP_DB_BOOTSTRAP=false /app/entrypoint.sh true && python -m app.society.seed'`
bootstraps an empty database, runs `alembic upgrade head` (head `0010_self_development`) and the
idempotent Society fleet seed in a separate container before the new deployment starts; a non-zero exit blocks the deployment
(`set -e`). Every other container built from the registry image — the registry's own runtime
container and the society worker — starts with `SKIP_DB_BOOTSTRAP=true`, which makes
`entrypoint.sh` exec its command without touching the schema, so a society-worker restart can never
race or repeat a migration. Local Compose and the fresh-install proof leave the variable unset
(default: bootstrap + migrate at start, unchanged). Regression: `tests/test_railway_adaptation.py`
(both paths) and the unchanged `tests/test_db_parity.py::test_entrypoint_bootstraps_empty_database_end_to_end`.

### D4 — Society worker workspace: one volume, bootstrapped at runtime

Railway volumes attach only to the runtime container (never at build or pre-deploy time) and one
service per volume, so the trusted-base checkout cannot be baked into the image.
`services/registry/start-society-railway.sh` runs at container start: it clones `SOCIETY_REPO_URL`
(public HTTPS, credential-free — a URL containing `@` or a non-HTTPS scheme is refused) into
`SOCIETY_REPO_ROOT=/workspace/repo` when no `.git` exists, otherwise reuses the checkout; fetches
`origin/<SOCIETY_REPO_REF>`; aligns the detached checkout to `RAILWAY_GIT_COMMIT_SHA` when set (an
unreachable commit is a hard failure — the worker must never run on a base that differs from the
deployment); resets a dirty trusted checkout; prunes stale worktree metadata only (candidate
worktrees under `SOCIETY_WORKSPACE_ROOT=/workspace/worktrees` and `agentnet-auto/*` branches are
kept); logs the commit SHA only; never pushes or forces; then execs `python -m app.society.worker`,
which idles (`SOCIETY_RUNTIME_ENABLED=false`) while serving Prometheus metrics on 9101 — the deploy
healthcheck. `SOCIETY_MODEL_PROVIDER=scripted`; no model key exists. Regression:
`tests/test_railway_adaptation.py` (first start, reuse, SHA alignment, dirty reset, worktree
preservation, refusal paths, no push/force).

### D5 — Proxy trust: `X-Real-IP` opt-in, never `FORWARDED_ALLOW_IPS=*`

Railway's edge documents `X-Real-IP` (client address), `X-Forwarded-Proto` (always `https`),
`X-Forwarded-Host`, `X-Railway-Edge` and `X-Railway-Request-Id` — it publishes neither
`X-Forwarded-For` semantics nor an ingress CIDR. uvicorn's `--proxy-headers` honours
`X-Forwarded-For` only from peers in `FORWARDED_ALLOW_IPS`; the sole way to make it honour Railway's
edge would be `*`, and with `*` the pinned uvicorn (0.52) returns the **leftmost**, client-written
`X-Forwarded-For` entry. The registry keys anonymous rate limits by `request.client.host`, so `*`
would let any caller pick a fresh bucket per login/register request. Decision: `FORWARDED_ALLOW_IPS`
keeps its default (uvicorn never reads `X-Forwarded-For` on Railway) and the registry enables
`EdgeClientAddressMiddleware` (`services/registry/app/proxy_headers.py`) with `TRUST_X_REAL_IP=true`:
the client address is the last `X-Real-IP` header (non-IP values ignored), the scheme follows
`X-Forwarded-Proto`, `X-Forwarded-For` is never read. Boundary justification: port 8000 is reachable
only through Railway's edge, which sets `X-Real-IP`, and the environment's private mesh, whose only
peers are AgentNet's own services. Residual risk: a private-mesh peer could set `X-Real-IP`; every
such peer is first-party. Proof required after deployment (runbook §11): a request carrying forged
`X-Real-IP` and `X-Forwarded-For` headers must not obtain a fresh rate-limit bucket; if it does, set
`TRUST_X_REAL_IP=false` (fail closed: every external caller shares the edge's bucket) and record the
platform finding — never widen trust. Regression: `tests/test_proxy_headers.py` (including the
uvicorn `*` demonstration and the wiring order). Payment is private and keeps the default; the
dashboard keeps Flask `ProxyFix` at one hop (`BEHIND_PROXY=true`).

### D6 — Networking and origins

Databases stay private by default (no Public Access / TCP proxy, so no `*_PUBLIC_URL` exists).
`payment`, `worker` and `society-worker` have no public domain; `registry` and `dashboard` get
Railway-generated domains only — no custom domain, no DNS change. Service-to-service URLs use
`${{<service>.RAILWAY_PRIVATE_DOMAIN}}` (`<service>.railway.internal`; environments created after
2025-10-16 resolve IPv4 and IPv6, so uvicorn keeps `--host 0.0.0.0`). `CORS_ALLOWED_ORIGINS` is
exactly `https://${{dashboard.RAILWAY_PUBLIC_DOMAIN}}`; the registry's `PUBLIC_BASE_URL` is
`https://${{RAILWAY_PUBLIC_DOMAIN}}`. Private networking is runtime-only; the pre-deploy migration
reaches PostgreSQL through the reference variables, which resolve to the private host. The
dashboard reaches the registry through `REGISTRY_URL` — its client now honours that documented
variable (it silently read `API_BASE_URL` before Phase 4, which every compose file left unset).

### D7 — Secrets

`JWT_SECRET_KEY`, `FLASK_SECRET_KEY` and `INTERNAL_WORKER_TOKEN` are Railway shared variables of the
staging environment (Project Settings → Shared Variables), generated locally with
`openssl rand -hex 32` and referenced from the IaC (`ctx.shared.*`); they are never committed,
printed or requested from the user. PostgreSQL/Redis credentials reach the services only as
reference variables (`PGHOST`… / `REDISHOST`…). `INTERNAL_WORKER_TOKEN` is given to `payment` only:
the background worker never calls payment (verified — no payment URL or token use in
`services/worker`). No `SOCIETY_MODEL_API_KEY`, `LLM_API_KEY`, `SOCIETY_GITHUB_TOKEN`, App private
key or DeepSeek value exists anywhere in the topology (`tests/test_railway_adaptation.py` forbids
them in the IaC file). Retired VPS credentials are never used.

### D8 — Deploy gate, healthchecks, restarts

Autodeploy from `main` with **Wait for CI** enabled on every GitHub-sourced service:
`.github/workflows/ci.yml` triggers on `push` to `main`, Railway waits for the check-suite
conclusions and skips the deployment on failure (2 h limit). Watch paths per service
(`/services/<name>/**`, gitignore-style from the repository root; the society worker watches
`/services/registry/**`). Healthchecks are deploy gates, not monitoring: `registry` and `payment`
`/readyz` (SELECT 1 + Redis PING; 503 otherwise), `dashboard` `/healthz`, `worker` and
`society-worker` `/metrics`; Railway probes the injected `PORT`, which each service sets explicitly
(`8000`, `8001`, `8080`, `9100`, `9101`) because the images fix their ports. Restart policy:
`Always` where the plan allows it (unavailable on Free/Trial, where the default On Failure, max 10,
stays) — a dashboard setting. Rollback restores the previous deployment's image and variables;
migrations `0003`→`0010` are additive, so a code rollback runs against the newer schema.

### D9 — Cost pre-flight

The plan class could not be inspected (no Railway access). Documented limits: Free $0 (0.5 GB
volume), Hobby $5/month with $5 included usage (5 GB volume), Pro $20/seat (1 TB); Trial volumes are
deleted 30 days after credits expire. The IaC requests a 2 048 MB society volume; on Free/Trial it is
reduced to 512 MB (edit `sizeMB`) rather than upgrading the plan. Five services plus two databases
is the whole staging footprint; no add-on, seat or tier change is made without explicit approval.

### D10 — `main` ruleset gate

Re-attempted in this phase: `POST /repos/vansyson1308/agentnet/rulesets` is refused by the
session's GitHub proxy (HTTP 403, write access to this API path not permitted), `github.com/login`
is egress-blocked, and no `gh` or browser session exists. Status stays
`MAIN RULESET: OWNER ACTION REQUIRED` with the exact payload in `deploy/github/main-ruleset.json`;
the code-layer refusals of ADR-0005 D7 remain the control.

### D11 — Blocker and resumption (historical; superseded by D12 the same day)

Verdict at the time of writing: `MANAGED STAGING — PARTIAL / BLOCKED`. Everything that does not need
Railway was done and merged; nothing had been created on Railway because no Railway endpoint was
reachable and no Railway tool was enabled in the session. Either remedy unblocks the bring-up without
any secret being pasted:

1. connect the **Railway** connector in claude.ai (OAuth to Railway's MCP server, served through
   Anthropic's MCP proxy, so the container egress policy does not apply), or
2. allow `railway.com`, `*.railway.com`, `railway.app`, `*.railway.app`, `backboard.railway.com`
   and `*.up.railway.app` in the Claude Code environment's network policy and complete
   `railway login --browserless` pairing when asked.

Then `docs/RAILWAY_STAGING.md` is executed top to bottom, twice (§20), before `GREEN` is claimed.

### D12 — Bring-up outcome (2026-09-18, connector)

Remedy 1 of D11 happened: the Railway connector was connected, the owner created the `staging` environment
and approved the Hobby plan, and the runbook was executed through the connector. Decisions taken while
executing, all of which keep D1–D10 intact:

* **Secrets are Railway-generated** (`${{secret(64, "abcdef0123456789")}}` shared variables) instead of
  `openssl rand` on a laptop: no value ever exists outside Railway's store and the connector only returns
  variable names. D7 unchanged in substance.
* **Pre-deploy also seeds the fleet**: `… && python -m app.society.seed` after `alembic upgrade head`. The seed
  is idempotent (unions operator gates, reuses existing agents) and runs in the same single migration-owner step,
  so D3's "exactly one owner" still holds and runtime containers still never touch the schema.
* **Validation runs inside the environment**: egress from the engineering session to `*.up.railway.app` stays
  blocked, so `deploy/railway/validate_staging.py` runs as a `staging-validator` service (registry image, clones
  `main`, private DNS to every service, database checks with the `POSTGRES_*` references, an operator per run,
  spoof test through the public edge). It is a validation harness, not a runtime dependency: no domain, no
  Society flag, `ON_FAILURE` restarts.
* **Three allow-listed operators** on `staging.agentnet.io.vn` (`EmailStr` rejects special-use domains) so
  consecutive validations use distinct ingress actors; the red-team burst spends an actor's hourly quota.
* **Wait for CI is an owner action**: `source.checkSuites` does not persist through the connector or the
  Railway agent; the repository's own merge discipline is the gate until the owner flips it in the dashboard.
* **Redeploy semantics**: a fresh deployment is produced by a variable write with a new value; `redeploy`
  copies the last successful snapshot and refuses a `SKIPPED` latest record; `restart-service` keeps the
  rendered environment. Documented in the runbook so no one "restarts" to pick up a config change.
* **Not run** (need `railway ssh`, which the connector lacks): the volume marker file and the `kill 1` crash
  test; replaced by the bootstrap's `cloning` / `reusing persistent checkout` log lines across a restart and a
  fresh deployment, and by the `ALWAYS` restart policy.

Evidence: `docs/RAILWAY_STAGING.md` (inventory, per-section "as executed" notes, §20 table).

## Official documentation consulted (source `railwayapp/docs@63cab08`, 2026-09-17)

`docs.railway.com` is egress-blocked; the same content was read from `content/docs/…` in the
published source repository.

| Source (`content/docs/…`) | What was verified | Used in |
| --- | --- | --- |
| `config-as-code.md` | deprecated; hard cutoff 2026-12-01; new services cannot opt in | D1 |
| `infrastructure-as-code.md`, `infrastructure-as-code/reference.md`, `cli/config.md` | `.railway/railway.ts`; `defineRailway`, `project`, `service`, `github(repo,{branch,rootDirectory})`, `postgres`, `redis`, `volume(name,{sizeMB})`, `volumeMounts`, `healthcheck`, `healthcheckTimeout`, `preDeploy`, `env`, `ctx.shared`; `railway config plan` / `apply` | D1 |
| `volumes.md`, `volumes/reference.md`, `variables/reference.md` | one service per volume; runtime-only mount; `RAILWAY_VOLUME_MOUNT_PATH`; Free 0.5 GB / Hobby 5 GB / Pro 1 TB; Trial deletion after 30 days | D4, D9 |
| `overview/advanced-concepts.md`, `infrastructure-as-code/reference.md` | pre-deploy command runs in a separate container before the deployment (no volume), must exit 0; pre-deploy timeout 1–3600 s | D3 |
| `deployments/healthchecks.md` | any 2xx passes; probes the injected `PORT`; default 300 s; hostname `healthcheck.railway.app`; deploy gate only | D8 |
| `deployments.md`, `overview/production-readiness-checklist.md` | restart policy (default On Failure, max 10; `Always` unavailable on Free/Trial); rollback restores image + variables | D8 |
| `databases/postgresql.md`, `databases/redis.md` | `PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE DATABASE_URL`; `REDISHOST REDISPORT REDISUSER REDISPASSWORD REDIS_URL`; private by default — Public Access creates a TCP proxy + `DATABASE_PUBLIC_URL` | D6, D7 |
| `networking/private-networking.md`, `networking/private-networking/how-it-works.md` | `<service>.railway.internal`; IPv4 + IPv6 for environments created after 2025-10-16; runtime-only | D6 |
| `networking/public-networking/specs-and-limits.md` | edge request headers `X-Real-IP`, `X-Forwarded-Proto` (always https), `X-Forwarded-Host`, `X-Railway-Edge`, `X-Request-Start`, `X-Railway-Request-Id`; no `X-Forwarded-For` contract | D5 |
| `deployments/github-autodeploys.md` | Wait for CI flag in service settings (workflow on push to the deployed branch; check-suite conclusions; failure skips; 2 h); contributor access required for autodeploys | D8 |
| `builds/build-configuration.md`, `deployments/monorepo.md`, `builds/dockerfiles.md` | root directory; watch paths (gitignore-style, evaluated from `/` even with a root directory); Dockerfile start-command override | D2, D8 |
| `variables.md`, `variables/reference.md` | shared variables (Project Settings → Shared Variables, per environment); `${{Service.VAR}}` / `${{shared.VAR}}`; `RAILWAY_PUBLIC_DOMAIN`, `RAILWAY_PRIVATE_DOMAIN`, `RAILWAY_GIT_COMMIT_SHA`, `RAILWAY_ENVIRONMENT_NAME` | D4, D6, D7 |
| `cli.md`, `cli/login.md`, `cli/variable.md`, `cli/ssh.md` | `railway login [--browserless]`; `railway variable set KEY --stdin -s <service>`; `railway ssh [COMMAND]` (SSH key registered on first use) | D7, runbook |

## Consequences

* The repository is deployable on Railway from `main` with no secret in git and no code path that
  migrates twice; local Compose behaviour is unchanged.
* On Railway the society worker runs on a clean, aligned, credential-free checkout of the deployed
  commit; candidate work survives restarts on the volume; the runtime stays OFF.
* Client-IP trust is explicit and testable per platform (`TRUST_X_REAL_IP`); the rate-limit bypass
  that `FORWARDED_ALLOW_IPS=*` would have created is documented and rejected.
* The staging environment exists and is `GREEN` (D12): two consecutive full validations on the same
  `main` commit through the in-environment validator, restart / persistence / rollback / secret-leak /
  resource proofs recorded in `docs/RAILWAY_STAGING.md`; production remains `NOT STARTED`, DNS unchanged.
