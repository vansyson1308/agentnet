# legacy/society_sqlite (superseded)

This is the earlier, standalone "Agent Society Backend" prototype: an in-memory
state dict persisted to SQLite, a DeepSeek proxy, and its own WebSocket. It was
never wired into docker-compose and hard-coded `/opt/agentnet/.env`.

It is superseded by the **Autonomous Society Runtime v1** in
`services/registry/app/society/` (durable Postgres events/runs/intents, policy
engine, isolated Builder/QA loop) — see `docs/SOCIETY_RUNTIME.md`.

Kept for reference only; the permission model in `models.py` (role permission
lists, no self-approval, human-only deploy actions) informed the new
`policy.py`. Do not run it in production.
