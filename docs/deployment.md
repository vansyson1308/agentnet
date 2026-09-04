# Deployment

The current, hosting-neutral deployment contract is **`docs/DEPLOYMENT_ARCHITECTURE.md`**
(components, environment groups, local/staging Compose projects, authorization model).
Decisions and verified references: `docs/adr/0003-prelive-deployment-hardening.md`.
Serverless suitability: `docs/VERCEL_COMPATIBILITY.md` (assessment only; nothing is deployed).

Quick facts:

* `docker-compose.yml` = **local development only** (project `agentnet-local`).
* `docker-compose.staging.yml` = standalone staging project (`agentnet-staging`) with
  externally managed Postgres/Redis; never combine it with `docker-compose.yml`.
* There is **no current production definition**. The retired single-VPS/SSH procedure
  (Vultr/Oracle VM, Caddy, Cloudflare tunnel, `/opt/agentnet`) is archived under
  `deploy/legacy-vps/` and refuses to run.
* Live model: NOT YET PROVEN. A2A v1: NOT STARTED. Final hosting: NOT SELECTED.
