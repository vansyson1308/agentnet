# deploy/legacy-vps — LEGACY, DO NOT USE FOR CURRENT DEPLOYMENT

These files document the retired single-VPS deployment model (one fixed host,
`root@` SSH access, a checkout under `/opt/agentnet`, Caddy or a Cloudflare tunnel
in front, and a combined `docker compose -f docker-compose.yml -f docker-compose.prod.yml`
project that also owned every local-development service). They are kept as
operational history only.

| File | What it was |
| --- | --- |
| `docker-compose.prod.yml` | Production overlay for the base file (project `agentnet-legacy-prod` now; never combined with `docker-compose.yml` again) |
| `runbook-prod.sh` | Production deploy on the VPS (backup, tag, compose up, Caddy reload) |
| `runbook-staging.sh` | Staging deploy on the same VPS, sharing its Postgres/Redis |
| `setup-oracle.sh` | First-time Oracle Cloud VM bootstrap |
| `Caddyfile` | Reverse proxy routing for the public and staging hostnames |
| `tunnel-config.yml` | Cloudflare tunnel ingress for the demo path |

Every shell script here refuses to run unless
`AGENTNET_ALLOW_LEGACY_VPS=I_UNDERSTAND_THIS_IS_RETIRED` is set, and
`tests/test_compose_topology.py` asserts that nothing outside this directory
references them.

Current material: `docs/DEPLOYMENT_ARCHITECTURE.md` (hosting-neutral component
contract), `docker-compose.yml` (local development only), `docker-compose.staging.yml`
(standalone staging project), `deploy/society-*` (platform-neutral checks).
