# Railway managed staging — runbook (Phase 4)

```
MANAGED STAGING — GREEN   (2026-09-18, main ae42d7a, two consecutive full validations — §20)
```

Repository side: **complete and merged** (ADR-0006, `.railway/railway.ts`, `tests/test_railway_adaptation.py`,
`tests/test_proxy_headers.py`, `tests/test_railway_validator.py`). Railway side: **deployed and validated** —
project `AgentNet` (`4a40abc4-f650-406d-be04-3d3c27ffc7b1`), environment `staging`
(`c8018c6d-c585-4d08-a25d-da3110a447cc`), executed through the Railway MCP connector (§19 records how the earlier
blocker was lifted and what the connector can and cannot do). Every command below has a CLI form for an operator
laptop and, where it differs, the connector form that was actually used; the engineering session never needed a
token pasted into it.

Live inventory (Railway-generated domains only; `agentnet.io.vn` untouched):

| Service | Service id | Exposure | Runtime image / command |
| --- | --- | --- | --- |
| Postgres | `7536b2c0-02da-4752-bb5c-85dfd2e6a695` | private only (no TCP proxy) | `ghcr.io/railwayapp-templates/postgres-ssl:18`, 5 GB volume |
| Redis | `3d40ae2e-3fa6-4a0c-b25c-de725f2d37b9` | private only (no TCP proxy) | `redis:8.2`, volume |
| registry | `ec110f06-c322-4362-9979-b87ecae183cf` | `https://registry-staging-145d.up.railway.app` | pre-deploy `sh -c 'SKIP_DB_BOOTSTRAP=false /app/entrypoint.sh true && python -m app.society.seed'`; runtime `SKIP_DB_BOOTSTRAP=true`; `TRUST_X_REAL_IP=true` |
| payment | `72e8ff98-0561-4350-a879-59f13414df2b` | private (`payment.railway.internal:8001`) | uvicorn |
| worker | `6286a7e2-3e38-4cdd-8ff9-346fa12746a1` | private (`worker.railway.internal:9100`) | `python -m app.worker` |
| society-worker | `9e3f3df3-d6ef-4ba2-b315-c1bac90ad456` | private (`society-worker.railway.internal:9101`) | `sh /app/start-society-railway.sh`, volume `society-workspace` (`e33794f0-795b-4839-90cc-6d199a2a33f9`) at `/workspace` |
| dashboard | `a12960db-ed8f-44da-b0ae-8d0e63909efa` | `https://dashboard-staging-4767.up.railway.app` | Flask (`REGISTRY_URL=http://registry.railway.internal:8000`) |
| staging-validator | `e38efc81-9598-4fe1-b7f0-e0b2c630d5c0` | private, no domain | §21 — clones `main`, runs `deploy/railway/validate_staging.py` |

## 0. Invariants (never change)

* Staging only: project `AgentNet`, environment `staging`. The default `production` environment is never used
  and never deployed to (`PRODUCTION DEPLOYMENT: NOT STARTED`).
* Railway-generated domains only. `agentnet.io.vn` (and `dashboard.` / `payment.` / `staging.`) and
  `139.180.143.222` are not touched (`DNS CHANGED: NO`).
* Society runtime OFF: `SOCIETY_RUNTIME_ENABLED=false`, `SOCIETY_AUTONOMOUS_CODE_ENABLED=false`,
  `SOCIETY_STAGING_DEPLOY_ENABLED=false`, `SOCIETY_PROMOTION_PROVIDER=disabled`,
  `SOCIETY_GITHUB_CREDENTIAL_PROVIDER=disabled`, `SOCIETY_AUTO_MERGE_ENABLED=false`,
  `SOCIETY_DEPLOYMENT_PROVIDER=disabled`, `SOCIETY_MODEL_PROVIDER=scripted`.
* No model key (`SOCIETY_MODEL_API_KEY` / `LLM_API_KEY` are never created — `DEEPSEEK KEY PRESENT: NO`), no
  GitHub credential (`SOCIETY GITHUB SECRET PRESENT: NO`), no plan upgrade or paid add-on without explicit
  approval, no retired-VPS credential.
* Secrets are generated locally and live only in Railway's variable store: never in git, chat, logs or reports.
* Deploy only validated `main` (Wait for CI). Never live-patch a container: fix on a branch → tests → PR →
  merge → post-merge CI → autodeploy.

## 1. Topology

| Service | Source (root directory) | Start | Exposure | `PORT` | Healthcheck | Volume |
| --- | --- | --- | --- | --- | --- | --- |
| `postgres` | managed PostgreSQL | — | private | — | — | managed |
| `redis` | managed Redis | — | private | — | — | managed |
| `registry` | `services/registry` (Dockerfile) | image `CMD` (uvicorn, `--proxy-headers`); **pre-deploy** `sh -c 'SKIP_DB_BOOTSTRAP=false /app/entrypoint.sh true && python -m app.society.seed'` (schema + idempotent fleet seed) | **public** (generated domain) | 8000 | `/readyz` | — |
| `payment` | `services/payment` | image `CMD` | private | 8001 | `/readyz` | — |
| `worker` | `services/worker` | image `CMD` | private | 9100 | `/metrics` | — |
| `dashboard` | `services/dashboard` | image `CMD` (`flask run`) | **public** (generated domain) | 8080 | `/healthz` | — |
| `society-worker` | `services/registry` | `sh /app/start-society-railway.sh` | private | 9101 | `/metrics` | `society-workspace` → `/workspace` |

No `simulation` service and no Jaeger (`JAEGER_ENABLED=false`; spans still persist in PostgreSQL). Postgres and
Redis reach the services only as reference variables (`${{Postgres.PGHOST}}` …); every service-to-service URL is a
`${{<service>.RAILWAY_PRIVATE_DOMAIN}}` template. The dashboard's public origin is the registry's only CORS origin.

## 2. Prerequisites and cost pre-flight

1. Railway account; the Railway GitHub App installed on `vansyson1308/agentnet`; the Railway user is a project
   member with contributor access (required for autodeploys).
2. Railway CLI: `npm i -g @railway/cli`, then `railway login` (browser) or `railway login --browserless`
   (pairing code). `railway whoami` must succeed. No `RAILWAY_API_TOKEN` is ever pasted into a chat.
3. Project `AgentNet` with an environment named `staging` (Dashboard → project → Settings → Environments → New).
   If the project does not exist, create an **empty** project named `AgentNet` first.
4. **Cost pre-flight** (record the answers in the report): Dashboard → Account → Plan. Free/Trial: reduce the
   society volume to 512 MB before applying (`sizeMB: 512` in `.railway/railway.ts`; Free volumes are 0.5 GB)
   and accept the default restart policy (`Always` is unavailable there). Hobby ($5/month, 5 GB volumes) or Pro:
   keep the file as is. Never upgrade a plan, buy an add-on or change tiers without explicit approval.

## 3. One-time secrets (shared variables of the `staging` environment)

Generate each value locally and enter it in **Dashboard → project → Settings → Shared Variables → environment
`staging`** (name, value, `Add`). The IaC references them as `${{shared.NAME}}`.

```bash
openssl rand -hex 32   # JWT_SECRET_KEY        — registry, payment, society-worker
openssl rand -hex 32   # FLASK_SECRET_KEY      — dashboard
openssl rand -hex 32   # INTERNAL_WORKER_TOKEN — payment only (nothing else calls payment)
```

As executed through the connector the three values were never generated on any laptop: each shared variable was
created with Railway's own generator (`${{secret(64, "abcdef0123456789")}}`), which renders a fresh 64-hex value
inside Railway's variable store; the connector returns variable *names* only, so no value was ever read back. The
validator's `STAGING_VALIDATOR_SECRET` (§21) was created the same way.

Do not create `SOCIETY_MODEL_API_KEY`, `LLM_API_KEY`, `SOCIETY_GITHUB_TOKEN`, `SOCIETY_GITHUB_APP_PRIVATE_KEY_*`
or any DeepSeek value. If a per-service variable is ever needed instead, it goes in through stdin:
`openssl rand -hex 32 | railway variable set NAME --stdin -s <service>` — never as a command-line literal.

## 4. Apply the topology

```bash
git checkout main && git pull --ff-only          # deploy only validated main
cd .railway && npm install && cd ..              # DSL type definitions (devDependencies only)
railway link --project AgentNet --environment staging
railway config plan                              # review: 2 databases, 5 services, 1 volume, no secrets
railway config apply                             # after confirmation
railway status
```

`railway.ts` refuses any environment other than `staging`. The first deployments start as soon as the GitHub
sources are attached; they fail their healthchecks until §5 is complete — expected.

## 5. Settings the DSL cannot express (dashboard / CLI, per service)

| Setting | Where | Value |
| --- | --- | --- |
| Public domain | `railway domain -s registry`, `railway domain -s dashboard` (or Service → Settings → Networking → Generate Domain) | Railway-generated only; **no** domain on payment, worker, society-worker |
| Wait for CI | Service → Settings → Source (flag appears once the workflow triggers on push to `main`) | ON for registry, payment, worker, dashboard, society-worker |
| Watch paths | Service → Settings → Source | registry `/services/registry/**`; payment `/services/payment/**`; worker `/services/worker/**`; dashboard `/services/dashboard/**`; society-worker `/services/registry/**` |
| Restart policy | Service → Settings → Deploy | `Always` if the plan allows it, otherwise default (On Failure, max 10) — record which |
| Pre-deploy timeout | registry → Settings → Deploy | 900 s (a stuck migration fails the deployment instead of hanging) |
| Database networking | postgres / redis → Settings → Networking | **no Public Access** (no TCP proxy; `DATABASE_PUBLIC_URL` must not exist) |

Then `railway redeploy -s <service>` for each service (registry first — its pre-deploy step owns the schema).

As executed: every row above is live. **Wait for CI** could not be set through the connector (the connector's
`update-service` and the Railway agent both accept `source.checkSuites` but the value does not persist); the
owner switched it on in the dashboard (Service → Settings → Source → *Wait for CI*) on 2026-09-19 and
`describe-service` now reports `checkSuites: true` for registry, payment, worker, dashboard and society-worker,
so a push to `main` deploys only after the GitHub check suite succeeds.
Restart policy is `ALWAYS` on the five services (Hobby plan); the validator uses `ON_FAILURE`, max 3 retries.
Watch paths are gitignore-style from the repository root (`/services/registry/**`, …, `/deploy/railway/**` for
the validator); a push that matches none of a service's patterns creates a `SKIPPED` deployment record for it.

## 6. Non-secret variables (declared in `.railway/railway.ts`)

Common to registry, payment, worker, society-worker: `ENVIRONMENT=staging`, `JAEGER_ENABLED=false`,
`POSTGRES_HOST/PORT/USER/PASSWORD/DB` ← `${{Postgres.PG*}}`, `REDIS_HOST/PORT/PASSWORD` ← `${{Redis.REDIS*}}`.
`FORWARDED_ALLOW_IPS` is deliberately **not** set (§11).

| Service | Variables |
| --- | --- |
| registry | `PORT=8000`, `SKIP_DB_BOOTSTRAP=true`, `TRUST_X_REAL_IP=true`, `JWT_SECRET_KEY=${{shared.JWT_SECRET_KEY}}`, `JWT_ALGORITHM=HS256`, `JWT_EXPIRATION=3600`, `PUBLIC_BASE_URL=https://${{RAILWAY_PUBLIC_DOMAIN}}`, `CORS_ALLOWED_ORIGINS=https://${{dashboard.RAILWAY_PUBLIC_DOMAIN}}`, `RATE_LIMIT_PER_MINUTE=60`, `ORCHESTRATOR_ENABLED=false`, `PUBLIC_AGENT_REGISTRATION_ENABLED=false`, `AUTO_SCALER_ENABLED=false`, `SOCIETY_OPERATOR_BOOTSTRAP_EMAILS=` (§9), Society OFF set — during a live window the registry mirrors the worker's NON-secret live flags (`SOCIETY_RUNTIME_ENABLED`, `SOCIETY_MODEL_PROVIDER`, `SOCIETY_MODEL_BASE_URL`, `SOCIETY_MODEL_NAME`, profile / thinking / effort / output format, limits, budget) so `/v1/society/status` and `/config` tell the truth; it never receives `SOCIETY_MODEL_API_KEY` (runbook §0) |
| payment | `PORT=8001`, `JWT_SECRET_KEY`, `JWT_ALGORITHM=HS256`, `INTERNAL_WORKER_TOKEN=${{shared.INTERNAL_WORKER_TOKEN}}` |
| worker | `PORT=9100`, `WORKER_METRICS_PORT=9100`, `WORKER_POLL_INTERVAL_SEC=30`, `REGISTRY_API_URL=http://${{registry.RAILWAY_PRIVATE_DOMAIN}}:8000` |
| dashboard | `ENVIRONMENT=staging`, `PORT=8080`, `FLASK_RUN_PORT=8080`, `BEHIND_PROXY=true`, `FLASK_SECRET_KEY=${{shared.FLASK_SECRET_KEY}}`, `REGISTRY_URL=http://${{registry.RAILWAY_PRIVATE_DOMAIN}}:8000` |
| society-worker | `PORT=9101`, `SOCIETY_METRICS_PORT=9101`, `SKIP_DB_BOOTSTRAP=true`, `JWT_SECRET_KEY`, `SOCIETY_REPO_ROOT=/workspace/repo`, `SOCIETY_WORKSPACE_ROOT=/workspace/worktrees`, `SOCIETY_REPO_URL=https://github.com/vansyson1308/agentnet.git`, `SOCIETY_REPO_REF=main`, `SOCIETY_WORKER_ID=railway-staging-society-worker`, `SOCIETY_MODEL_OUTPUT_FORMAT=auto`, `SOCIETY_HEARTBEAT_INTERVAL_SECONDS=3600`, Society OFF set |

## 7. Health matrix (first validation gate)

```bash
R=https://$(railway domain -s registry | tail -1)      # or copy the generated domains from the dashboard
D=https://$(railway domain -s dashboard | tail -1)
curl -sS $R/healthz; curl -sS $R/readyz                 # {"status":"ok"} / {"status":"ready"}
curl -sS $D/healthz; curl -sS $D/readyz                 # readyz proves dashboard → registry over the private mesh
railway ssh -s payment        -- python -c "import urllib.request as u;print(u.urlopen('http://localhost:8001/readyz').status)"
railway ssh -s worker         -- python -c "import urllib.request as u;print(u.urlopen('http://localhost:9100/metrics').status)"
railway ssh -s society-worker -- python -c "import urllib.request as u;print(u.urlopen('http://localhost:9101/metrics').status)"
```

Record for each service: build OK, pre-deploy OK (registry), healthcheck passed, `railway logs -s <svc>` free of
tracebacks. A healthcheck passing is a deploy gate, not monitoring — restarts after a crash are covered by §14.

## 8. Schema proof (exactly one migration owner)

```bash
railway logs -s registry | grep -E "alembic|db_bootstrap|SKIP_DB_BOOTSTRAP"   # pre-deploy: bootstrap/upgrade; runtime: SKIP line
railway ssh -s registry -- sh -c 'cd /app && alembic current'                  # 0011_expire_rehearsal_memory (head)
railway logs -s registry | grep "society seed report"                         # pre-deploy: fleet created once, reused afterwards
railway ssh -s society-worker -- sh -c 'echo SKIP_DB_BOOTSTRAP=$SKIP_DB_BOOTSTRAP'   # true
railway logs -s society-worker | grep -c alembic                                     # 0
```

## 9. Staging operator (structural; the token is never reported)

SMTP is not wired, so email verification is completed on the staging database. The registry allow-lists three
bootstrap operators (`SOCIETY_OPERATOR_BOOTSTRAP_EMAILS=staging-operator@staging.agentnet.io.vn,staging-operator-b@staging.agentnet.io.vn,staging-operator-c@staging.agentnet.io.vn`)
so that consecutive validations use distinct ingress actors: the red-team burst (`--burst 40`) spends an actor's
hourly ingress quota (`SOCIETY_INGRESS_MAX_PER_ACTOR_PER_HOUR=30`), so a run that reuses an actor within the hour
reports `BREACH A06a,A10` (the quota, not a defence, is what refuses the events) — that is a validator-side
false alarm, and the fix is a fresh actor or a one-hour gap, never a change to the guard. The operator used by a
run is `VALIDATOR_OPERATOR_EMAIL` on the validator service (§21). The addresses are synthetic strings on the project's own staging subdomain: pydantic's email validator rejects special-use domains such as `.local`, `.invalid` and `.test`, and no mail is ever sent.
The in-Railway validator (§21) performs these steps itself; by hand they are:

```bash
curl -sS -X POST $R/v1/auth/register -H 'content-type: application/json' -d '{"email":"staging-operator@staging.agentnet.io.vn","password":"<local-only>"}'
railway connect postgres            # psql: UPDATE users SET is_email_verified = true WHERE email = 'staging-operator@staging.agentnet.io.vn';
curl -sS -X POST $R/v1/auth/user/login -H 'content-type: application/json' -d '{"email":"staging-operator@staging.agentnet.io.vn","password":"<local-only>"}'
```

Keep the returned `access_token` in the shell only (`export SOCIETY_SMOKE_TOKEN=…`). A durable role for a real
person is assigned with `railway ssh -s registry -- python -m app.society.operator_auth <email> operator`. The
report states `STAGING OPERATOR: CREATED` — never the token.

## 10. Society smoke and red-team (scripted only — `SCRIPTED — NOT LIVE MODEL`, `LIVE MODEL: NOT RUN`)

```bash
python deploy/society-staging-smoke.py --api $R --expect-runtime off --report staging-smoke.json
SOCIETY_REDTEAM_TOKEN=$SOCIETY_SMOKE_TOKEN python deploy/society-staging-redteam.py --api $R --burst 40 --report staging-redteam.json
```

(`SOCIETY_REDTEAM_USER_TOKEN` / `SOCIETY_REDTEAM_AGENT_TOKEN` are a plain user and an agent created for the
run.) The `--metrics-probe` option needs the private network; the equivalent is the society-worker `/metrics`
line in §7. Core application smoke: `GET $R/v1/agents`, the dashboard landing page, login page and marketplace
over `$D`.

## 11. Proxy-header spoof test (ADR-0006 D5 — required before GREEN)

`TRUST_X_REAL_IP=true` makes the registry take the client address from `X-Real-IP`, which Railway's edge sets;
`X-Forwarded-For` is never trusted. The test proves a caller cannot mint fresh rate-limit buckets:

```bash
burst() { for i in $(seq 1 60); do curl -s -o /dev/null -w '%{http_code}\n' -X POST $R/v1/auth/user/login \
  -H 'content-type: application/json' "$@" -d '{"email":"nobody@example.invalid","password":"x"}'; done | sort | uniq -c; }
burst                                                                     # baseline: N×401 then 429s
burst -H "X-Forwarded-For: 203.0.113.$RANDOM" -H "X-Real-IP: 198.51.100.$RANDOM"   # forged headers
```

PASS: the forged run reaches `429` no later than the baseline (same bucket). FAIL (a forged `X-Real-IP` reaches
the app unchanged): set `TRUST_X_REAL_IP=false` on the registry, redeploy, record the platform finding, and
keep it OFF — never set `FORWARDED_ALLOW_IPS=*`.

## 12. Private-network audit

* `railway domain -s payment`, `-s worker`, `-s society-worker` → no domain; the services answer only via
  `railway ssh` / private DNS.
* postgres / redis: Settings → Networking shows no Public Access; `railway variable list -s postgres` has no
  `DATABASE_PUBLIC_URL`; `railway variable list -s redis` has no `REDIS_PUBLIC_URL`.
* `curl -sS $D/readyz` → `ready` (dashboard → registry over `registry.railway.internal`).
* `railway variable list -s <svc>` for each service: names only are checked — no `SOCIETY_MODEL_API_KEY`,
  `LLM_API_KEY`, `SOCIETY_GITHUB_TOKEN`, `*_PRIVATE_KEY*`, `DEEPSEEK*`; `SOCIETY_*` flags exactly as §0.

As executed (connector `list-domains`, `list-tcp-proxies`, `list-variables`, 2026-09-18 16:19 UTC): payment,
worker, society-worker and staging-validator have no service or custom domain; Postgres and Redis have no TCP
proxy (`proxies: []`) and no `*_PUBLIC_URL` variable; registry and dashboard carry exactly one Railway-generated
domain each; no service variable name matches `SOCIETY_MODEL_API_KEY`, `LLM_API_KEY`, `SOCIETY_GITHUB_TOKEN`,
`*PRIVATE_KEY*` or `DEEPSEEK*`. The validator's `H04` (`dashboard /readyz` → registry over private DNS) and
`H05`–`H08` (payment, worker, society-worker, registry over `*.railway.internal`) are the private-mesh proof.

## 13. Persistence proof (society workspace volume)

```bash
railway logs -s society-worker | grep -E "cloning|reusing existing checkout|trusted base checkout at"
railway ssh -s society-worker -- sh -c 'date -u > /workspace/.persistence-marker; git -C /workspace/repo rev-parse HEAD'
railway restart -s society-worker
railway ssh -s society-worker -- sh -c 'cat /workspace/.persistence-marker; git -C /workspace/repo rev-parse HEAD'   # marker kept; SHA = RAILWAY_GIT_COMMIT_SHA
railway redeploy -s society-worker
railway ssh -s society-worker -- cat /workspace/.persistence-marker                                                  # still there; log says "reusing existing checkout"
```

The bootstrap only ever resets the trusted checkout inside `/workspace/repo`; candidate worktrees under
`/workspace/worktrees` and anything else on the volume survive.

As executed: the connector has no `railway ssh`, so the marker-file step was **not run**; the proof is the
bootstrap's own log lines, which distinguish a first clone from a reused checkout. First deployment
(`84e1e23b`, commit `ac1b57e`): `cloning https://github.com/vansyson1308/agentnet.git (ref main) into
/workspace/repo` → `trusted base checkout at ac1b57ef…; workspace root /workspace/worktrees holds 0 candidate
worktree dir(s)`. After `restart-service` (16:26 UTC): `reusing persistent checkout at /workspace/repo` →
`trusted base checkout at ac1b57ef…` (same SHA as `RAILWAY_GIT_COMMIT_SHA` of that deployment). After a fresh
deployment from `main` (`ae42d7a`): `reusing persistent checkout` again, then the checkout is aligned to the new
deployment commit — see §20 for the deployment ids. Note that the connector's `redeploy` refuses a service whose
*latest* deployment record is `SKIPPED` (a push that matched none of its watch paths); a variable write with a
new value creates the fresh deployment instead.

## 14. Restart / failure proof

```bash
for s in registry payment worker dashboard society-worker; do railway restart -s $s; done   # all healthy again (§7)
railway ssh -s worker -- kill 1                                                             # container exits → restart policy brings it back
railway logs -s worker | tail -20                                                           # new start, no traceback
```

As executed: `restart-service` on registry, payment, worker, dashboard and society-worker (connector, in place,
no rebuild). Each logged a graceful shutdown (`Registry service shutdown`, `Payment service shutdown`,
`Auto-Refund Worker stopped (graceful)`, `society worker stopped (graceful)`) followed by one clean start, no
traceback, and `environment-status` reported all replicas `running` with zero failures; the registry restart
logged `SKIP_DB_BOOTSTRAP=true — … starting: uvicorn` (no migration on restart). The `kill 1` crash test needs
`railway ssh` and was **not run** through the connector; the restart policy is `ALWAYS` on all five services
(`describe-service`), which is what would bring a crashed container back.

## 15. Rollback readiness

Service → Deployments → previous successful deployment → **Rollback** (restores that image and its
variables). Migrations `0003`→`0010` are additive, so the previous code runs against the newer schema. Record
the deployment IDs of the last two green deployments per service in the report.

As executed (`list-deployments status=SUCCESS`, 2026-09-18): registry `342011b2` (ae42d7a, current) ← earlier
green deployments of `ac1b57e`; dashboard `5e67fbd6` (ae42d7a); payment `5052662e` (ac1b57e); worker `1affb4d0`
(ac1b57e); society-worker `84e1e23b` (ac1b57e) then the fresh `ae42d7a` deployment of §13; Postgres `61e0c69f`;
Redis `51becd6b`; staging-validator `7b306615` (run 1) and `94f6d61b` (run 2). Rollback is Service → Deployments
→ *Rollback* on the previous green record; no rollback was needed.

## 16. Log / secret audit

```bash
for s in registry payment worker dashboard society-worker; do
  railway logs -s $s | grep -Eic 'sk-[a-z0-9]{8}|ghs_|ghp_|github_pat_|BEGIN (RSA |OPENSSH |EC )?PRIVATE|x-access-token|PGPASSWORD=|JWT_SECRET_KEY=|Bearer [A-Za-z0-9._-]{20,}' ; done
```

Every count must be `0` → `SECRET LEAK CHECK: PASS`; otherwise `FAIL`, rotate the affected value and fix the
logging before continuing. Values are never printed while searching or reporting.

As executed: the connector's `get-logs` filter (`password`, `secret`, `Bearer`, `ghp_`, `PGPASSWORD`,
`JWT_SECRET`) returned no line for any service, and the complete deploy logs of every service since its first
start (registry pre-deploy included, both validator runs included) were read: they contain health probes, the
alembic and seed reports (agent ids only), the bootstrap SHA lines and the validator's `CHECK` lines — no
credential, token or password value. `SECRET LEAK CHECK: PASS`.

## 17. Resource sanity

Dashboard → service → Metrics: record CPU, memory and (society-worker) volume usage after 30 minutes idle.
Expected: idle memory well under the plan's per-service limit, volume usage a few hundred MB (one checkout),
no restart loop (`railway logs` shows one start per service).

As executed (`get-service-metrics`, one hour ending 16:19 UTC, limit 8 GB per service on Hobby): registry
≈ 0.09 GB (peak 0.22 GB during the validator bursts), payment ≈ 0.07 GB, worker ≈ 0.05 GB, society-worker
≈ 0.08 GB with 0.12 GB on the volume (one checkout), dashboard ≈ 0.03 GB, Postgres ≈ 0.06 GB / 0.16 GB disk,
Redis ≈ 0.04 GB / 0.08 GB disk; CPU averages below 1 % of a vCPU everywhere; one start per service per
deployment (no restart loop).

## 18. Build failure policy

A failing build or pre-deploy is fixed in the repository (branch → tests → PR → merge → post-merge CI →
autodeploy). Nothing is patched inside a container; `railway redeploy` is used only to rerun an unchanged
deployment (for example after a variable change).

## 19. Blocker history and what the connector can and cannot do (state on 2026-09-18)

The earlier blocker (every Railway host `CONNECT 403` from the engineering session, no connector) was lifted by
the first remedy: the **Railway** connector was connected in claude.ai, the project `AgentNet` was created through
it, and the owner created the `staging` environment and selected the Hobby plan (explicitly approved; the trial had
expired). Everything after that ran through the connector's tools. Facts learned while executing, so that the
next operator does not rediscover them:

* **Staged changes** (`connect-service-source`, `update-service`, `set-variables` with `staged: true`) are
  committed by asking the Railway agent to "commit the staged patch"; the connector's `accept-deploy` call times
  out without effect.
* **A variable write with a *new* value creates a deployment**; writing the same value again deploys nothing.
  `redeploy` copies the latest *successful* snapshot (it does not pick up a changed pre-deploy command or
  variable) and refuses when the latest record is `SKIPPED`; `restart-service` restarts the container with the
  environment it was deployed with. To roll a config change out, write a variable with a new value.
* **`source.checkSuites` (Wait for CI) does not persist** through the connector or the agent → owner action (§5).
* **Environments cannot be created** through the connector or the agent (the owner created `staging`).
* **No `railway ssh` / `railway connect`**: the marker-file and `kill 1` steps (§13, §14) are not run; the
  database-side checks (§8, §9) run inside the environment as the `staging-validator` service (§21).
* Egress from the engineering session to `*.up.railway.app` stays blocked, so nothing is curled from the
  session; the validator does it from inside the private network and reports through its logs.

## 21. Running the validations from inside Railway (`staging-validator`)

An operator laptop can run §7–§17 by hand; the reproducible way — and the only way from an engineering session
whose egress cannot reach `*.up.railway.app` — is the `staging-validator` service: the registry image (root
directory `/services/registry`) with the start command

```
sh -c 'rm -rf /tmp/repo && git clone -q --depth 1 --branch "$VALIDATOR_REF" https://github.com/vansyson1308/agentnet.git /tmp/repo && python /tmp/repo/deploy/railway/${VALIDATOR_SCRIPT:-validate_staging.py}; echo "validator finished with exit $?"; exec tail -f /dev/null'
```

(`VALIDATOR_SCRIPT` unset → the full validation; `VALIDATOR_SCRIPT=phase5_live.py` → the Phase 5 live-society
driver, docs/SOCIETY_LIVE_MODEL_RUNBOOK.md §3.1. `VALIDATOR_EXPECT_RUNTIME=on|off` tells the validation which
public `runtime_enabled` flag to assert — `off` unless a live window is open.)

and the variables `REGISTRY_PUBLIC_URL`, `DASHBOARD_PUBLIC_URL`, the `POSTGRES_*` references, the Railway-generated
shared secret `STAGING_VALIDATOR_SECRET` (`${{secret(64, "abcdef0123456789")}}`, never read back),
`VALIDATOR_OPERATOR_EMAIL`, `VALIDATOR_USER_EMAIL`, `VALIDATOR_REF=main`, `EXPECTED_ALEMBIC_HEAD`, `REDTEAM_BURST`.
`deploy/railway/validate_staging.py` runs §7 (public through the edge, private through private DNS), §8 (on the
database), §9, §10, the core smoke and §11, prints `CHECK <id> PASS|FAIL …` lines and `VALIDATION RESULT: GREEN|RED`,
then keeps the container alive. A new run is a fresh deployment of the validator, triggered by writing
`VALIDATOR_RUN=<n>` with a value not used before (a restart would re-run the *same* rendered environment, and an
identical value deploys nothing — §19); set `VALIDATOR_OPERATOR_EMAIL` to an allow-listed operator that has not
been used within the hour in the same write. The deployment's logs carry `VALIDATOR start deployment=… commit=…
operator=…`, one `CHECK <id> PASS|FAIL` line per check and `VALIDATION RESULT: GREEN|RED (26 checks)`. §13–§17
stay connector/CLI steps (logs, restart, redeploy, metrics). No secret ever reaches the logs.

## 20. Two consecutive full validations (required for GREEN)

Both runs are on `main` `ae42d7a177a8c94eb33636a91d700b4e6eed9520` (CI run 96 green), 2026-09-18, through the
`staging-validator` service (§21) plus the connector steps of §12–§17. Validator lines are quoted verbatim.

| Check | Run 1 — deployment `7b306615`, operator `staging-operator-b`, 16:20 UTC | Run 2 — deployment `94f6d61b`, operator `staging-operator-c`, 16:23 UTC |
| --- | --- | --- |
| §7 health matrix (5 services) | `H01`–`H08` PASS (registry public `/healthz` `/readyz`, dashboard public `/healthz`, dashboard `/readyz` → registry over private DNS, payment / worker / society-worker / registry over `*.railway.internal`) | `H01`–`H08` PASS |
| §8 schema proof (`0010_self_development`, one owner) | `S01 PASS alembic_version=['0010_self_development']`, `S02 PASS 42 public tables`; registry pre-deploy log: bootstrap → stamp `0003` → upgrade → `0010` on the first deployment, `alembic already stamped — running upgrade` (no-op) + `society seed … reused=[7 agents] grants=7` on every later one; runtime containers log `SKIP_DB_BOOTSTRAP=true` | same (`S01`, `S02` PASS; pre-deploy no-op + seed reuse) |
| §9 operator created (structural) | `O01a PASS register staging-operator-b: created`, `O01b` verified on the database, `O01c PASS login: HTTP 200`, `O02 PASS operator surface /v1/society/config HTTP 200`; `U01a`–`U01c` plain user, `U02 PASS plain user refused on operator surface HTTP 403` | `O01a PASS register staging-operator-c: created`, `O01b`, `O01c`, `O02`, `U01a`–`U02` PASS |
| §10 smoke `--expect-runtime off` + red-team | `M01 PASS society smoke exit 0` (`C01`–`C10` PASS, `C11`/`C12` SKIP by design, `SOCIETY SMOKE: PASS`, `runtime_enabled=False`, fleet present); `R01 PASS society red-team exit 0` (`SOCIETY RED-TEAM: ALL DEFENDED`) — `SCRIPTED — NOT LIVE MODEL`, `LIVE MODEL: NOT RUN` | `M01` PASS, `R01` PASS (`ALL DEFENDED`) |
| core smoke + dashboard | `C01 PASS public agent listing HTTP 200`, `C02`–`C04` dashboard `/`, `/landing`, `/metaverse` HTTP 200 | same |
| §11 spoof test PASS | `P01 PASS baseline first 429 at #75, forged first 429 at #1` (forged `X-Forwarded-For`/`X-Real-IP` earn no fresh bucket) | `P01 PASS baseline first 429 at #101, forged first 429 at #1` |
| verdict line | `VALIDATION RESULT: GREEN (26 checks)` | `VALIDATION RESULT: GREEN (26 checks)` |
| §12 private-network audit | PASS — no domain on payment / worker / society-worker / validator, no TCP proxy on Postgres / Redis, no forbidden variable name (connector listings, 16:19 UTC) | re-read after run 2: unchanged |
| §13 persistence (restart + redeploy) | restart 16:26 UTC: `reusing persistent checkout at /workspace/repo` → `trusted base checkout at ac1b57ef…` (= deployment commit); fresh deployment `780b3f48` from `ae42d7a` 16:29 UTC: `reusing persistent checkout` → `trusted base checkout at ae42d7a1…` | restart 16:31 UTC (deployment `780b3f48`): `reusing persistent checkout` → `ae42d7a1…`; connector `redeploy` → deployment `3e321ee7` 16:33 UTC: `reusing persistent checkout at /workspace/repo` → `trusted base checkout at ae42d7a1…; … 0 candidate worktree dir(s)` |
| §14 restart / failure | `restart-service` registry, payment, worker, dashboard, society-worker 16:25–16:26 UTC: graceful stop, one clean start each, all replicas running, zero failures | same cycle 16:31 UTC: all replicas running, zero failures |
| §15 rollback readiness recorded | previous green deployment per service recorded (see §15) | unchanged |
| §16 `SECRET LEAK CHECK: PASS` | PASS (filters empty, full logs read) | PASS (validator run 2 logs read: `CHECK` lines only) |
| §17 resource sanity | recorded (§17): idle memory 0.03–0.09 GB per service, volume 0.12 GB, no restart loop | unchanged |

Post-restart health matrix: a third validator deployment (`04f03d10`, `VALIDATOR_RUN=4`, operator
`staging-operator` — its hourly quota had lapsed) ran at 16:55 UTC after both restart cycles, the fresh
deployment and the redeploy: `H01`–`H08`, `S01`–`S02`, `O01a`–`O02`, `U01a`–`U02`, `M01`, `R01` (`ALL DEFENDED`),
`C01`–`C04`, `P01 PASS baseline first 429 at #75, forged first 429 at #1` — `VALIDATION RESULT: GREEN (26 checks)`.

Only two consecutive clean runs on the same `main` commit yield `MANAGED STAGING — GREEN`; anything less stays
`PARTIAL / BLOCKED` with the failing row named. **Verdict: `MANAGED STAGING — GREEN`.**

## 22. Phase 5 live window (2026-09-19) — runtime ON with a real model

The Society runtime and the autonomous code loop were enabled on this environment against real
DeepSeek. Full evidence: `docs/SOCIETY_LIVE_PROOF.md`. What this section records is the
environment's behaviour, not the Society's.

| Check | Result |
| --- | --- |
| Two consecutive full validations, distinct actors (`staging-operator-b`, `staging-operator`) | run 24 RED on `S01` only, run 25 **GREEN (26 checks)** |
| `S01` on run 24 | the DATABASE was correct at `0011_expire_rehearsal_memory`; the stale `EXPECTED_ALEMBIC_HEAD` **service variable** overrode the correct code default. Reconciled; run 25 PASS |
| Society smoke, runtime ON | `SOCIETY SMOKE: PASS` — `C04 runtime_enabled == True` asserted, `C08` operator config redacts the credential, production deploy OFF |
| Red-team, runtime ON, live model | `SOCIETY RED-TEAM: ALL DEFENDED`, both runs |
| Proxy-header spoof (`P01`) | baseline first 429 at #73, **forged** first 429 at #1 — a forged `X-Forwarded-For` buys no fresh rate-limit bucket |
| Wait for CI | **verified active** — every deployment sat `WAITING` until `main` CI passed, on registry and society-worker alike |
| Migration ownership | unchanged: the registry pre-deploy is the only migration owner; head `0011_expire_rehearsal_memory` |
| Resource / cost | 57 live runs, $0.048 of a $1.00 daily model budget; 0 retries, 0 timeouts |

Two environment-level lessons:

1. **`EXPECTED_ALEMBIC_HEAD` has two sources of truth** — the code default in
   `deploy/railway/validate_staging.py` and the service variable, and the variable wins silently.
   Update both when a migration lands.
2. **The red-team burst is expensive with the runtime ON.** It spends the actor's hourly ingress
   quota (§9) *and* drives the fleet into its per-role hourly run limit; the window recorded 34
   runs correctly skipped with `global runs/hour limit reached (30/30)`. Use a fresh operator per
   run and expect the skip reasons.
