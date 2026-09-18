# Railway managed staging — runbook (Phase 4)

```
MANAGED STAGING — PARTIAL / BLOCKED   (2026-09-18)
```

Repository side: **complete and merged** (ADR-0006, `.railway/railway.ts`, `tests/test_railway_adaptation.py`,
`tests/test_proxy_headers.py`). Railway side: **not started** — no Railway endpoint is reachable from the
engineering session and no Railway connector is enabled (§19). This runbook is what gets executed, verbatim and
twice (§20), once the blocker is lifted. Every command below is run by the operator from a laptop with the
Railway CLI; the engineering session never needs a token pasted into it.

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
| `registry` | `services/registry` (Dockerfile) | image `CMD` (uvicorn, `--proxy-headers`); **pre-deploy** `sh -c 'SKIP_DB_BOOTSTRAP=false /app/entrypoint.sh true'` | **public** (generated domain) | 8000 | `/readyz` | — |
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

## 6. Non-secret variables (declared in `.railway/railway.ts`)

Common to registry, payment, worker, society-worker: `ENVIRONMENT=staging`, `JAEGER_ENABLED=false`,
`POSTGRES_HOST/PORT/USER/PASSWORD/DB` ← `${{Postgres.PG*}}`, `REDIS_HOST/PORT/PASSWORD` ← `${{Redis.REDIS*}}`.
`FORWARDED_ALLOW_IPS` is deliberately **not** set (§11).

| Service | Variables |
| --- | --- |
| registry | `PORT=8000`, `SKIP_DB_BOOTSTRAP=true`, `TRUST_X_REAL_IP=true`, `JWT_SECRET_KEY=${{shared.JWT_SECRET_KEY}}`, `JWT_ALGORITHM=HS256`, `JWT_EXPIRATION=3600`, `PUBLIC_BASE_URL=https://${{RAILWAY_PUBLIC_DOMAIN}}`, `CORS_ALLOWED_ORIGINS=https://${{dashboard.RAILWAY_PUBLIC_DOMAIN}}`, `RATE_LIMIT_PER_MINUTE=60`, `ORCHESTRATOR_ENABLED=false`, `PUBLIC_AGENT_REGISTRATION_ENABLED=false`, `AUTO_SCALER_ENABLED=false`, `SOCIETY_OPERATOR_BOOTSTRAP_EMAILS=` (§9), Society OFF set |
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
railway ssh -s registry -- sh -c 'cd /app && alembic current'                  # 0010_self_development (head)
railway ssh -s society-worker -- sh -c 'echo SKIP_DB_BOOTSTRAP=$SKIP_DB_BOOTSTRAP'   # true
railway logs -s society-worker | grep -c alembic                                     # 0
```

## 9. Staging operator (structural; the token is never reported)

SMTP is not wired, so email verification is completed on the staging database. The registry allow-lists two
bootstrap operators (`SOCIETY_OPERATOR_BOOTSTRAP_EMAILS=staging-operator@agentnet.local,staging-operator-b@agentnet.local`)
so that two consecutive validations use distinct ingress actors (the red-team burst consumes an actor's hourly quota).
The in-Railway validator (§21) performs these steps itself; by hand they are:

```bash
curl -sS -X POST $R/v1/auth/register -H 'content-type: application/json' -d '{"email":"staging-operator@agentnet.local","password":"<local-only>"}'
railway connect postgres            # psql: UPDATE users SET is_email_verified = true WHERE email = 'staging-operator@agentnet.local';
curl -sS -X POST $R/v1/auth/user/login -H 'content-type: application/json' -d '{"email":"staging-operator@agentnet.local","password":"<local-only>"}'
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

## 14. Restart / failure proof

```bash
for s in registry payment worker dashboard society-worker; do railway restart -s $s; done   # all healthy again (§7)
railway ssh -s worker -- kill 1                                                             # container exits → restart policy brings it back
railway logs -s worker | tail -20                                                           # new start, no traceback
```

## 15. Rollback readiness

Service → Deployments → previous successful deployment → **Rollback** (restores that image and its
variables). Migrations `0003`→`0010` are additive, so the previous code runs against the newer schema. Record
the deployment IDs of the last two green deployments per service in the report.

## 16. Log / secret audit

```bash
for s in registry payment worker dashboard society-worker; do
  railway logs -s $s | grep -Eic 'sk-[a-z0-9]{8}|ghs_|ghp_|github_pat_|BEGIN (RSA |OPENSSH |EC )?PRIVATE|x-access-token|PGPASSWORD=|JWT_SECRET_KEY=|Bearer [A-Za-z0-9._-]{20,}' ; done
```

Every count must be `0` → `SECRET LEAK CHECK: PASS`; otherwise `FAIL`, rotate the affected value and fix the
logging before continuing. Values are never printed while searching or reporting.

## 17. Resource sanity

Dashboard → service → Metrics: record CPU, memory and (society-worker) volume usage after 30 minutes idle.
Expected: idle memory well under the plan's per-service limit, volume usage a few hundred MB (one checkout),
no restart loop (`railway logs` shows one start per service).

## 18. Build failure policy

A failing build or pre-deploy is fixed in the repository (branch → tests → PR → merge → post-merge CI →
autodeploy). Nothing is patched inside a container; `railway redeploy` is used only to rerun an unchanged
deployment (for example after a variable change).

## 19. Blocker and the exact unblock (state on 2026-09-18)

Every Railway host (`railway.com`, `railway.app`, `docs.railway.com`, `backboard.railway.com`,
`cli.railway.com`, `mcp.railway.com`, `*.up.railway.app`) returns `CONNECT 403` from the engineering session's
organisation egress policy, and no Railway MCP connector is enabled for the session. Nothing was created on
Railway. One of these user actions lifts it — no token is pasted anywhere:

1. **Connect the Railway connector in claude.ai** (Settings → Connectors → Railway → Connect; OAuth in the
   browser). It is served through Anthropic's MCP proxy, so the container egress policy does not apply. Then
   re-run the Phase 4 mission; the session executes this runbook with the connector's tools.
2. **Or** allow `railway.com`, `*.railway.com`, `railway.app`, `*.railway.app`, `backboard.railway.com` and
   `*.up.railway.app` in the Claude Code environment's network policy, then re-run the mission and complete the
   `railway login --browserless` pairing code when the session asks for it.

## 21. Running the validations from inside Railway (`staging-validator`)

An operator laptop can run §7–§17 by hand; the reproducible way — and the only way from an engineering session
whose egress cannot reach `*.up.railway.app` — is the `staging-validator` service: the registry image (root
directory `/services/registry`) with the start command

```
sh -c 'rm -rf /tmp/repo && git clone -q --depth 1 --branch "$VALIDATOR_REF" https://github.com/vansyson1308/agentnet.git /tmp/repo && python /tmp/repo/deploy/railway/validate_staging.py; echo "validator finished with exit $?"; exec tail -f /dev/null'
```

and the variables `REGISTRY_PUBLIC_URL`, `DASHBOARD_PUBLIC_URL`, the `POSTGRES_*` references, the Railway-generated
shared secret `STAGING_VALIDATOR_SECRET` (`${{secret(64, "abcdef0123456789")}}`, never read back),
`VALIDATOR_OPERATOR_EMAIL`, `VALIDATOR_USER_EMAIL`, `VALIDATOR_REF=main`, `EXPECTED_ALEMBIC_HEAD`, `REDTEAM_BURST`.
`deploy/railway/validate_staging.py` runs §7 (public through the edge, private through private DNS), §8 (on the
database), §9, §10, the core smoke and §11, prints `CHECK <id> PASS|FAIL …` lines and `VALIDATION RESULT: GREEN|RED`,
then keeps the container alive so `railway restart -s staging-validator` (or the connector's restart) is run 2. Run 2
uses the second allow-listed operator (`VALIDATOR_OPERATOR_EMAIL=staging-operator-b@agentnet.local`). §13–§17
stay connector/CLI steps (logs, restart, redeploy, metrics). No secret ever reaches the logs.

## 20. Two consecutive full validations (required for GREEN)

| Check | Run 1 | Run 2 |
| --- | --- | --- |
| §7 health matrix (5 services) | | |
| §8 schema proof (`0010_self_development`, one owner) | | |
| §9 operator created (structural) | | |
| §10 smoke `--expect-runtime off` + red-team | | |
| §11 spoof test PASS | | |
| §12 private-network audit | | |
| §13 persistence (restart + redeploy) | | |
| §14 restart / failure | | |
| §15 rollback readiness recorded | | |
| §16 `SECRET LEAK CHECK: PASS` | | |
| §17 resource sanity | | |

Only two consecutive clean runs on the same `main` commit yield `MANAGED STAGING — GREEN`; anything less stays
`PARTIAL / BLOCKED` with the failing row named.
