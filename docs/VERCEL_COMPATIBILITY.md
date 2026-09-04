# Vercel compatibility assessment (document only — nothing is deployed)

Assessed 2026-09-04 against Vercel material reachable from the build sandbox: the
`vercel/vercel` and `vercel/workflow` repositories, npm/PyPI metadata and docs bundled in
the `workflow` package, plus search excerpts of vercel.com pages that were egress-blocked
(numeric plan limits are therefore marked *snippet* and must be re-checked on vercel.com
before any decision). No `vercel.json` is added; no project is created.

Platform facts relied on: Functions are short-lived, isolated invocations; Fluid compute is
the default (many invocations per instance); `maxDuration` caps every invocation (defaults
300 s, up to 800 s on Pro/Enterprise, 1800 s beta — *snippet*); `waitUntil` extends **the
current invocation** only and is bounded by that `maxDuration` (it is not a daemon); Cron
Jobs are GETs to a production path on a schedule, may be **duplicated or missed** (no
exactly-once), hour-level accuracy on Hobby (*snippet*); WebSockets on Functions are a
2026 public beta bounded by `maxDuration`; filesystem is read-only except ephemeral `/tmp`
(*snippet* 500 MB); Python runtime supports ASGI apps (FastAPI) with streaming; Vercel
Workflows (durable, event-sourced steps with retries; Python SDK beta) exist for
long-running orchestration.

| Component | Classification | Why / what would have to change |
| --- | --- | --- |
| React/dashboard UI | **COMPATIBLE** (if it existed as a buildable app) | The only SPA sources in the repo are unbuildable fragments (`legacy/frontend-fragments/`); a future static frontend would deploy as static assets |
| Flask dashboard (`services/dashboard`) | **POSSIBLE WITH REFACTOR** | Stateless WSGI app would run as a Python Function, but it proxies to internal service URLs and assumes a long-lived session secret; templates are server-rendered per request (fine) |
| FastAPI Registry (`/v1/*` REST) | **POSSIBLE WITH REFACTOR** | ASGI runs on the Python runtime; needs an external connection pooler (functions are "not designed for persistent connections"), `alembic upgrade` moved out of the request path (a deploy step), and rate limiting backed by Redis rather than process memory |
| Payment API | **POSSIBLE WITH REFACTOR** | Same as the registry; money routes are transactional and short |
| Registry WebSocket (`/v1/ws/agent/{id}`, feed, timeline) | **NOT SUITABLE AS-IS** | Agent sessions and plaza state are held in one process (`websocket_manager`) with Redis fan-out; Functions WebSockets are beta, per-instance, and closed at `maxDuration`, so every agent would reconnect every ≤30 min and cross-instance state would need to move entirely into Redis |
| Society worker (`LISTEN society_wake` loop, leases, `FOR UPDATE SKIP LOCKED`) | **NOT SUITABLE AS-IS** | A LISTEN/NOTIFY loop cannot outlive an invocation; a Cron-driven `run_until_idle` would be at-least-once and unserialized (safe only because runs are lease-claimed and intents idempotent, but wall-clock bounded and imprecise on Hobby); Vercel Workflows could host the wake→claim→decide→execute chain only after a port to durable steps |
| Worker background loops (auto-refund, daily resets) | **POSSIBLE WITH REFACTOR** | Already idempotent and reconciliation-based; each loop iteration could be a Cron-invoked function guarded by `CRON_SECRET`, accepting duplicate/missed runs |
| Builder worktrees (git, isolated branches) | **NOT SUITABLE AS-IS** | Needs a writable, persistent checkout and the `git` binary; `/tmp` is ephemeral and size-capped |
| QA subprocesses (`python -m pytest` in the worktree) | **NOT SUITABLE AS-IS** | Long, CPU-bound child processes against a checkout; exceeds the invocation model |
| Local filesystem assumptions (`SOCIETY_WORKSPACE_ROOT`, worker backlog file) | **NOT SUITABLE AS-IS** | Must become object storage or a persistent volume elsewhere |
| Redis | **COMPATIBLE (external)** | Managed Redis-compatible service; nothing in Vercel itself |
| PostgreSQL | **COMPATIBLE (external)** | Managed Postgres with a pooler; migrations run from CI/CD, and `LISTEN/NOTIFY` consumers live outside Vercel |
| Cron | **POSSIBLE WITH REFACTOR** | Only for idempotent reconciliation work; never for exactly-once accounting |
| Live-model calls (`OpenAICompatibleModel`) | **COMPATIBLE** | Bounded retries and timeouts (`SOCIETY_MODEL_TIMEOUT_SECONDS` × attempts) fit within `maxDuration`; credential via project env |

Idempotency and concurrency implications if any Cron-driven piece is adopted: every handler
must be safe under duplicate invocation and overlap (the society runtime already is —
atomic claims, idempotent intents, event idempotency keys — but the general worker's
timeout refunds rely on row locks, which hold). Nothing here may be read as "Vercel gives us
a background daemon": it does not.

Likely future shape (not decided): static/UI and stateless APIs on a serverless platform,
with the Society worker, WebSocket hub and Builder on a persistent worker host (container
platform or VM) sharing the managed Postgres and Redis.
