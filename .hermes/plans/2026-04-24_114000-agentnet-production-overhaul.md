# AgentNet Production Overhaul Plan

**Author:** Hermes (DeepSeek V4 Flash)
**Date:** 2026-04-24 11:40
**Target:** vansyson1308/agentnet — Agent-to-Agent AI Economy Platform

---

## 1. Goal

Transform AgentNet from a proof-of-concept with missing deps and bootstrap issues into a **production-ready, deployable platform** that can run `docker compose up` end-to-end without errors, pass all tests, and serve as a million-dollar MVP.

---

## 2. Current Context

### 2.1 What's Good
- Clean microservices architecture: registry (8000), payment (8001), worker, simulation (8002), dashboard (8080), jaeger
- Robust escrow design via PostgreSQL triggers — app code only touches `reserved_*`, triggers handle `balance_*`
- A2A Agent Card support (RFC 8615)
- Sandbox isolation with SSRF protection
- WebSocket + Redis pub/sub real-time messaging
- 84+ existing tests in TASKLOG.md
- 5 Dockerfiles already exist (registry, payment, worker, simulation, dashboard)
- 7 init SQL scripts for schema + triggers
- Python SDK in `sdk/python/`

### 2.2 Issues Found

| # | Severity | Issue | File(s) |
|---|----------|-------|---------|
| P0 | **CRASH** | Worker tracing doesn't check `JAEGER_ENABLED` — always tries Jaeger export | `services/worker/app/tracing.py:24` |
| P0 | **CRASH** | `database.py` calls `load_dotenv()` but `python-dotenv` missing from registry/payment/simulation requirements | All `database.py` + `requirements.txt` |
| P0 | **CRASH** | `auth.py` imports `ed25519`, `jose`, `passlib` — missing from requirements | `services/registry/requirements.txt` |
| P0 | **CRASH** | `tracing.py` imports `opentelemetry-*` — missing from all requirements | All `services/*/requirements.txt` |
| P0 | **CRASH** | `websocket_manager.py` imports `redis.asyncio` — needs `redis` with async support | `services/registry/requirements.txt` có redis nhưng check phiên bản |
| P0 | **CRASH** | `sandbox.py` imports `httpx` — missing from registry requirements | `services/registry/requirements.txt` |
| P1 | **DATA** | SQL schema `task_sessions` missing `input` column (model Python có) | `01-init.sql` |
| P1 | **DATA** | SQL schema `task_sessions` missing `fulfillment_channel`, `retry_of_id`, `input` columns | `01-init.sql` |
| P1 | **DATA** | SQL schema `transactions` missing `platform_fee`, `platform_fee_rate`, `extra_data` (model có) | `01-init.sql` |
| P1 | **DATA** | SQL schema missing `negotiation_rounds`, `agent_interactions`, `notifications` tables | Missing init SQL |
| P1 | **RUNTIME** | `websocket_manager.py` imports `sqlalchemy.func` as `sql_func` — but never used in routes some cases | `websocket_manager.py:11` |
| P1 | **BUILD** | `docker-compose.demo.yml` and `docker-compose.prod.yml` not validated | Both files |
| P1 | **BUILD** | `deploy/` scripts reference `telegram-bot` service không tồn tại | `deploy/*` |
| P2 | **QUALITY** | `task_contract.py` tồn tại nhưng không được dùng trong tasks.py | `services/registry/app/task_contract.py` |
| P2 | **QUALITY** | `governance.py` tồn tại, `create_notification` được import nhưng chưa verify hoạt động | `tasks.py:27` |
| P2 | **QUALITY** | Duplicate model definitions across 3 services (registry, payment, worker) — risk of drift | All `models.py` |
| P2 | **QUALITY** | `pyproject.toml` thiếu cấu hình project thực sự (chỉ có black/isort) | Root `pyproject.toml` |
| P2 | **SECURITY** | `auth.py:22` lộ JWT_SECRET_KEY trong inline comment | `auth.py` |
| P2 | **SECURITY** | Admin user hardcoded password hash in SQL | `01-init.sql:343` |
| P3 | **DOCS** | `README.md` references `telegram-bot` không tồn tại | README.md |
| P3 | **DOCS** | `.env.example` mô tả biến `LLM_API_KEY` là `***` gây confusion | `.env.example` |
| P3 | **TEST** | `pytest.ini` không có async support config | `pytest.ini` |

---

## 3. Proposed Approach

### Strategy
1. **Fix P0 first** — dependencies, tracing crash, dotenv → so Docker compose builds cleanly
2. **Sync SQL schema with Python models** — prevent runtime data errors
3. **Bootstrap + verify** — run `docker compose up`, run demo script, verify end-to-end flow
4. **Enhance for production** — shared models, task_contract integration, security hardening
5. **Deploy configuration** — Cloudflare Tunnel / Render / VPS deployment blueprint

### Guiding Principle
- **Trigger-based escrow = source of truth.** Never touch `balance_*` in app code.
- **One model definition per table.** Duplicate models = death by drift.
- **Graceful degradation.** If Jaeger is down, services should still run.

---

## 4. Step-by-Step Plan

### Phase 1: Fix Dependencies & Runtime Crashes (P0)

#### 1.1 Standardize all `requirements.txt`

Each service needs a complete, tested set. Create shared base requirements:

**`requirements-base.txt`** (new — shared ref):
```
fastapi==0.104.1
uvicorn==0.24.0
sqlalchemy==2.0.23
psycopg2-binary==2.9.9
redis==5.0.1
python-dotenv==1.0.0
```

Then per-service additions:

**Registry (+):**
```
httpx==0.25.0
ed25519==1.5
python-jose[cryptography]==3.3.0
passlib[bcrypt]==1.7.4
bcrypt==4.0.1
jsonschema==4.20.0
opentelemetry-api==1.21.0
opentelemetry-sdk==1.21.0
opentelemetry-instrumentation-fastapi==0.42b0
opentelemetry-instrumentation-sqlalchemy==0.42b0
opentelemetry-exporter-jaeger-thrift==1.21.0
```

**Payment (+):** same as registry minus jsonschema, httpx, ed25519

**Worker (+):**
```
httpx==0.25.0
redis==5.0.1
python-dotenv==1.0.0
opentelemetry-api==1.21.0
opentelemetry-sdk==1.21.0
opentelemetry-exporter-jaeger-thrift==1.21.0
opentelemetry-instrumentation-sqlalchemy==0.42b0
```

**Simulation (+):** same as registry

**Dashboard:** keep Flask + httpx as-is

**Files to change:** All `services/*/requirements.txt`

#### 1.2 Fix Worker tracing.py

**Problem:** Always exports to Jaeger — crashes when Jaeger not available.

**Fix:** Add `JAEGER_ENABLED` check (same pattern as registry/payment).

**Files to change:** `services/worker/app/tracing.py`

#### 1.3 Add async test support to pytest.ini

```
asyncio_mode = auto
```

**Files to change:** `pytest.ini`

---

### Phase 2: Sync SQL Schema ↔ Python Models (P1)

#### 2.1 Fix `01-init.sql` — add missing columns

Add to `task_sessions`:
- `input JSONB` (after `input_hash`)
- `fulfillment_channel VARCHAR`
- `retry_of_id UUID REFERENCES task_sessions(id)`

Add to `transactions`:
- `platform_fee BIGINT DEFAULT 0`
- `platform_fee_rate NUMERIC(5,4) DEFAULT 0.025`
- `extra_data JSONB DEFAULT '{}'`

Add new tables:
- `negotiation_rounds` (from models.py)
- `agent_interactions` (from models.py)
- `notifications` (from models.py)

Also: update SQL in `02-platform-fee.sql` through `07-governance.sql` if they reference old schema.

**Files to change:** `services/registry/init-db/01-init.sql`, `02-platform-fee.sql`–`07-governance.sql`

#### 2.2 Create shared models module

**Idea:** Move all models to `services/registry/app/models.py` (it already has everything), have payment/worker import from it.

BUT: Docker compose has separate containers. Options:
- **Option A (recommended):** Install registry models as editable package in payment/worker Dockerfiles
- **Option B (simpler):** Copy `models.py` to each service (current approach) — verify they're in sync

**Decision:** Keep per-service models but add a CI check (`make validate-models`) that diffs them.

**Files to change:** `Makefile` (new target), add comment headers to each `models.py`

---

### Phase 3: Bootstrap & Verify End-to-End

#### 3.1 Build and start with Docker Compose

```bash
docker compose up -d --build
```

Verify:
- ✅ All containers `Up (healthy)` or `Up`
- ✅ `curl localhost:8000/health` → `{"status":"ok"}`
- ✅ `curl localhost:8001/health` → `{"status":"ok"}`
- ✅ Jaeger UI at `localhost:16686`

#### 3.2 Run demo end-to-end

```bash
pip install -r requirements-dev.txt
python examples/demo_end_to_end.py
```

Verify:
- ✅ User registration → token
- ✅ Agent creation → agent ID
- ✅ Wallet funding → balance
- ✅ Task creation → escrow locked
- ✅ Task completion → escrow released
- ✅ Trace queryable

#### 3.3 Run all tests

```bash
pytest tests/ -v
```

Expected: 84+ passing. Fix any regressions.

---

### Phase 4: Enhance for Production (P2)

#### 4.1 Integrate `task_contract.py`

The file exists at `services/registry/app/task_contract.py` with `TaskCreateRequest`, `ExecuteParams`, `validate_state_transition()`, `compute_input_hash()`. But `tasks.py` and `websocket_manager.py` use their own inline validation.

**Fix:** Refactor `tasks.py` and `websocket_manager.py` to use `task_contract.py` as the single source of truth for:
- Task creation request validation
- State transition validation (guards against invalid status changes)
- Input hashing (replace `hash_input` in `auth.py`)

**Files to change:** `services/registry/app/tasks.py`, `services/registry/app/websocket_manager.py`

#### 4.2 Security hardening

- Remove hardcoded admin password hash from `01-init.sql:343` (or move to env-conditional)
- Remove JWT_SECRET_KEY inline value in `auth.py:22` (currently shows `os.get...EY` — already truncated, but confirm)
- Add `CORS_ALLOWED_ORIGINS` validation in production mode

#### 4.3 `.env.example` cleanup

- Remove `***` placeholders, use descriptive `your_*` placeholders consistently
- Add comments for which vars are required vs optional

---

### Phase 5: Deployment Blueprint

#### 5.1 Production docker-compose.prod.yml

Currently exists but untested. Update to:
- Remove `--reload` (dev mode)
- Add restart policies
- Add resource limits
- Use `.env` properly
- Remove volume mounts for source code (use built images)

#### 5.2 Deployment targets

| Option | Cost | Complexity | Best For |
|--------|------|------------|----------|
| Docker VPS (Vultr $6/mo) | $6/mo | Medium | Full control |
| Render Docker | $7/mo | Low | Quick deploy |
| Cloudflare Tunnel + VPS | $6+Vultr | Medium | Vietnam access |

All documented in `docs/deployment.md`.

---

## 5. Files Likely to Change

```
services/registry/requirements.txt       — add all deps
services/payment/requirements.txt        — add all deps
services/worker/requirements.txt         — add all deps
services/simulation/requirements.txt     — add all deps
services/worker/app/tracing.py           — add JAEGER_ENABLED check
services/registry/init-db/01-init.sql    — add missing columns + tables
services/registry/init-db/*.sql          — review + sync
services/registry/app/tasks.py           — integrate task_contract.py
services/registry/app/websocket_manager.py — integrate task_contract.py
services/registry/app/auth.py            — remove inline secret reference
pytest.ini                               — add asyncio_mode
Makefile                                 — add validate-models target
.env.example                             — cleanup placeholders
docker-compose.prod.yml                  — update for production
docs/deployment.md                       — add deployment guide
```

## 6. Tests & Validation

| Stage | Command | Expected |
|-------|---------|----------|
| Build | `docker compose build` | All 5 services build without error |
| Config | `docker compose config` | Valid compose file |
| Run | `docker compose up -d` | All containers healthy |
| Health | `curl localhost:8000/health` | `{"status":"ok"}` |
| Health | `curl localhost:8001/health` | `{"status":"ok"}` |
| E2E | `python examples/demo_end_to_end.py` | Full cycle success |
| Unit | `pytest tests/ -v` | 84+ passing |
| Money | `pytest tests/test_money_invariants.py -v` | 6/6 passing |

## 7. Risks & Tradeoffs

| Risk | Mitigation |
|------|------------|
| Opentelemetry version conflicts | Pin exact versions, test in Docker |
| bcrypt C extension compile failure | Use `bcrypt==4.0.1` with pre-built wheel |
| Redis async driver mismatch | `redis==5.0.1` supports asyncio natively |
| Shared models drift | Add CI check, or move to shared package |
| Demo script stale | Update after schema changes |
| Docker build cache issues | Use `--no-cache` during initial verification |

## 8. Open Questions

1. **Simulation service** có thực sự cần trong MVP không? Nó phụ thuộc vào LLM API key.
2. **Dashboard** viết bằng Flask — có nên migrate sang FastAPI cho đồng bộ?
3. **Admin user seed data** — có nên loại bỏ khỏi init SQL và thêm CLI command?
4. **Hedera integration** — có tiếp tục extension path hay bỏ hẳn?
