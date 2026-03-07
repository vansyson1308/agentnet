# CLAUDE.md — AgentNet Protocol v2.0 (Claude Code Operating Guide)

This file provides **project-specific guidance** for Claude Code (claude.ai/code) when working with this repository.
AgentNet is an **escrow + wallet** system: correctness and invariants are more important than new features.

---

## 1) Project Overview

AgentNet is a microservices platform for AI agents (AgentNet Protocol v2.0). The platform enables agents to:
- register and authenticate users/agents
- discover each other and exchange offers/referrals
- execute tasks with **escrow-based payments**
- stream updates via WebSocket + Redis pub/sub
- record traces/spans for observability

**Prime directive:** Never introduce money duplication, escrow bypass, or inconsistent wallet states.

---

## 2) Tech Stack

- **Framework**: FastAPI (Python)
- **Database**: PostgreSQL 15 (SQLAlchemy ORM)
- **Cache / Pub-Sub**: Redis 7
- **Tracing**: Jaeger (OpenTelemetry)

---

## 3) Safety / Non-negotiables (Escrow + Wallet)

This repo involves financial invariants. Follow these rules strictly:

### Wallet & transactions
- **Wallet balances must be updated in exactly ONE place**:
  - either PostgreSQL triggers OR application code, **not both**.
- Any change touching `wallets`, `transactions`, triggers, or completion/refund logic MUST include:
  - a regression test, and
  - a short "money invariant" note (what stays true, and how verified).

### Escrow consistency
- WS and REST must enforce the **same** escrow rules:
  - reserve/lock escrow
  - spending cap checks
  - consistent task session state transitions
  - real `input_hash` (no placeholders)

### Observability must be real
- If spans/trace IDs are generated, they must be **persisted** and queryable (no "TODO-only tracing").

### Security baseline
- Do not ship with allow-all CORS outside local dev.
- Add basic rate limiting before any public exposure.
- Prefer parameterized SQL; avoid string interpolation for queries.

### Task Execution Contracts (non-negotiable)
- Use `services/registry/app/task_contract.py` for all task validation:
  - `TaskCreateRequest` - REST task creation (strict, rejects unknown fields)
  - `ExecuteParams` - WebSocket task execution
  - `validate_state_transition()` - single source of truth for state machine
  - `compute_input_hash()` - deterministic hashing (rejects NaN/Infinity)
- Canonical JSON: keys sorted, UTF-8, no NaN/Infinity

### Secrets
- Never commit secrets. Keep `.env.example` updated whenever env vars change.

---

## 4) Work Order (Follow this priority)

### P0 — Fix correctness BEFORE features
1) Prevent double wallet/balance updates (trigger vs app code).
2) Ensure WebSocket execution path cannot bypass escrow locking.
3) Ensure spans/traces are persisted (no placeholder logic).
4) Fix runtime errors from missing imports / undefined references.
5) Verify auth flows: user registration/login endpoints exist and work.

### P1 — Make repo runnable end-to-end
- ✅ `docker compose config` succeeds.
- ✅ Dockerfiles created for: registry, payment, worker.
- ✅ telegram-bot and dashboard removed from compose (not in source).
- `.env.example` exists and matches requirements.
- **Tests exist**: run `pytest tests/ -v` to verify invariants.

### P2 — Tests (money path first)
- Unit tests:
  - escrow lock/release/refund
  - wallet invariants (no double credit)
  - auth (user + agent)
  - WS vs REST parity
  - tracing persistence
- Integration stage:
  - gateway → redis → worker → db → notify stub

### P3 — MVP gaps / product completeness
- Dashboard (balance/history/traces/agent management) - not in source
- SDK + sample agents
- Remaining WS methods (if spec requires)
- Input schema validation at task execution time
- Rate limiting + CORS tightening for non-dev

---

## 5) Commands

### 5.1 Docker Development (recommended)

```bash
# Validate compose file(s) — passes ✅
docker compose config

# Build and start all services
docker compose up -d --build

# Check service status
docker compose ps

# Tail logs
docker compose logs -f registry
docker compose logs -f payment
docker compose logs -f worker
docker compose logs -f dashboard

# Stop / reset
docker compose down
```

### 5.2 Local Development (without Docker)

> Prefer a virtual environment. Install dependencies per service.

```bash
# Create .env from .env.example first
cp .env.example .env

# Registry Service
cd services/registry
python -m venv .venv
# Windows:
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Payment Service
cd services/payment
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload

# Worker
cd services/worker
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m app.worker
```

### 5.3 Testing / Verification

```bash
# Run all tests
pytest tests/ -v

# Quick test run
pytest -q

# Run specific test categories
pytest tests/test_money_invariants.py -v        # Escrow/wallet invariants
pytest tests/test_ws_escrow.py -v               # WebSocket escrow parity
pytest tests/test_tracing.py -v                # Tracing persistence
pytest tests/test_cors_security.py -v           # CORS security config
pytest tests/test_rate_limiting.py -v          # Rate limiting
pytest tests/test_task_contract.py -v           # Task contracts & state machine
pytest tests/test_approval_workflow.py -v         # Approval workflow tests

# Or use Makefile
make test        # Run tests
make format      # Format code
make lint        # Run linters
make compose-up  # Start services
```

---

## 6) Architecture

### 6.1 Services

| Service  | Port  | Purpose |
|---------|-------|---------|
| registry | 8000 | Agent registration, task management, auth, WebSocket |
| payment  | 8001 | Wallets, transactions, approval requests |
| worker   | -    | Background: auto-refund timeouts, daily metrics reset |
| jaeger   | 16686| Distributed tracing UI |

### 6.2 API Structure

**Registry Service** (`/api/v1/`)
- `POST /api/v1/auth/*` - Authentication (login, register, etc.)
- `GET/POST /api/v1/agents/*` - Agent CRUD and discovery
- `GET/POST /api/v1/tasks/*` - Task session management
- `WS /api/v1/ws` - WebSocket for real-time updates
- `GET /api/v1/tasks/traces/{trace_id}` - Retrieve spans for a trace

**Payment Service** (`/api/v1/`)
- `GET/POST /api/v1/wallets/*` - Wallet management
- `GET/POST /api/v1/transactions/*` - Transaction history
- `GET/POST /api/v1/approval_requests/*` - Approval workflow

### 6.3 Database schema (shared across services)

- `users` - User accounts with KYC status
- `agents` - Registered AI agents with capabilities, endpoint, public_key
- `wallets` - Dual-currency (credits + USDC), reserved funds, spending caps
- `task_sessions` - Task execution with escrow, timeout tracking
- `spans` - Distributed tracing data per agent (persisted)
- `transactions` - Payment ledger; triggers for balance updates
- `referrals` - Agent referral system with rewards
- `offers` - Task offers between agents

### 6.4 Key patterns

1. **Database triggers**: triggers enforce wallet balance updates / spending caps.
2. **Escrow system**: task payments held in reserved fields until completion.
3. **Timeout handling**: worker polls every 30s for timed-out tasks and issues refunds.
4. **WebSocket**: registry uses Redis pub/sub for real-time task notifications.

### 6.5 Shared models

Models are defined in `services/registry/app/models.py` and imported by other services.

### 6.6 SDK & Examples

- **Python SDK**: `sdk/python/agentnet/` - Client library for Registry + Payment services
- **Examples**: `examples/` - Demo scripts and sample agents

### 6.7 Dashboard

- **Dashboard Service**: `services/dashboard/` - Local dev telemetry UI
- Access at http://localhost:8080 when running `docker compose up`
- Features: Overview, Traces, Approvals

Run demo:
    python examples/demo_end_to_end.py

---

## 7) Environment Variables

Copy `.env.example` to `.env` and configure:
- `POSTGRES_*` - Database credentials
- `REDIS_*` - Redis credentials
- `JWT_SECRET_KEY`, `JWT_ALGORITHM`, `JWT_EXPIRATION` - Auth
- `JAEGER_AGENT_HOST`, `JAEGER_AGENT_PORT` - Tracing (optional)
- `ENVIRONMENT` - Set to production for production (default: development)
- `CORS_ALLOWED_ORIGINS` - Comma-separated allowed origins for production
- `RATE_LIMIT_PER_MINUTE` - Max requests per minute (default: 60)

**Rule:** If you add/change env vars, update `.env.example` and docs in the same change.

---

## 8) Service Dependencies

```
postgres ──┬── registry (port 8000)
           ├── payment (port 8001)
           └── worker
redis ────┬── registry (WebSocket/pubsub)
           ├── payment
           └── worker
jaeger ───┴── (tracing for all services)
```

---

## 9) How Claude Code should work here (mandatory dev loop)

For every task/change:

1) **Plan** (max 8 bullets):
   - files to touch
   - invariants to preserve (esp. escrow/wallet)
   - how you will verify (commands)
2) **Implement** small diff.
3) **Run** the relevant command(s):
   - at minimum: `docker compose config`
   - plus tests: `pytest tests/ -v`
4) **Fix** until verification is green.
5) **Summarize**:
   - changed files
   - what you verified
   - any follow-ups

**No large refactors** unless explicitly requested.
If anything is unclear, prefer "verify by reading code + running commands" over guessing.
