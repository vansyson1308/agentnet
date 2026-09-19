# deploy/railway — pointer

Railway staging is declared as Infrastructure as Code in **`.railway/railway.ts`** (Railway's IaC DSL; the
deprecated `railway.toml` / `railway.json` config-as-code is not used). The operator runbook — prerequisites,
cost pre-flight, secrets, apply, dashboard-only settings, the health/schema/persistence/restart/rollback/secret
audits, the proxy spoof test and the two-run validation table — is **`docs/RAILWAY_STAGING.md`**; the decisions
and the official-documentation citations are **`docs/adr/0006-railway-managed-staging.md`**.

Repository pieces that exist for Railway:

| Piece | Purpose |
| --- | --- |
| `.railway/railway.ts`, `.railway/package.json`, `.railway/README.md` | staging-only topology: managed Postgres/Redis, registry (public), payment, worker, dashboard (public), society-worker (+ one volume); no secret values |
| `services/registry/entrypoint.sh` (`SKIP_DB_BOOTSTRAP`) | exactly one migration owner: the registry pre-deploy command; runtime containers never migrate |
| `services/registry/start-society-railway.sh` | society worker volume bootstrap: credential-free clone/reuse, alignment to `RAILWAY_GIT_COMMIT_SHA`, stale-metadata prune only, never pushes |
| `services/registry/app/proxy_headers.py` (`TRUST_X_REAL_IP`) | client address from Railway's `X-Real-IP` instead of `FORWARDED_ALLOW_IPS=*` |
| `deploy/society-staging-smoke.py`, `deploy/society-staging-redteam.py` | scripted smoke / red-team against the public registry domain |
| `tests/test_railway_adaptation.py`, `tests/test_proxy_headers.py` | regressions for all of the above |

Quick sequence (details and every gate in the runbook):

```bash
railway login                                    # or --browserless
railway link --project AgentNet --environment staging
# create the three shared secrets in Project Settings → Shared Variables (openssl rand -hex 32 each)
cd .railway && npm install && cd .. && railway config plan && railway config apply
# per service: generated domain (registry, dashboard only), Wait for CI, watch paths, restart policy, pre-deploy timeout
```

Status: `MANAGED STAGING — GREEN` (2026-09-18, `main` ae42d7a) — `docs/RAILWAY_STAGING.md` has the live inventory, the evidence per section and the §20 validation table; `validate_staging.py` is what the `staging-validator` service runs (`VALIDATOR_EXPECT_RUNTIME` selects the runtime flag it asserts); `phase5_live.py` is the Phase 5 live-society driver the same service runs when `VALIDATOR_SCRIPT=phase5_live.py` (docs/SOCIETY_LIVE_MODEL_RUNBOOK.md §3.1).
