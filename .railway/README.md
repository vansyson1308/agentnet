# Railway Infrastructure as Code

`railway.ts` describes the **staging** environment of the `AgentNet` Railway project (services, managed
Postgres/Redis, the society worker's persistent volume, non-secret variables and reference variables).
It contains no secret values. Apply it with the Railway CLI from a linked checkout:

```bash
cd .railway && npm install          # type definitions for the DSL (package.json here)
railway login                       # or RAILWAY_API_TOKEN in the environment (never committed)
railway link --project AgentNet --environment staging
railway config plan                 # preview the diff against the live environment
railway config apply                # apply after confirmation
```

The file refuses to render for any environment other than `staging`. Everything the DSL cannot express
(generated domains, Wait for CI, watch paths, restart policy, pre-deploy timeout) is listed in
`docs/RAILWAY_STAGING.md`. Config-as-code (`railway.toml` / `railway.json`) is deprecated by Railway
(hard cutoff 2026-12-01) and is deliberately not used.
