# TASKLOG.md — Repo Truth Audit

**Date:** 2026-03-05
**Audit:** docker compose + service layout verification

---

## 1) docker compose config

```bash
docker compose config
```

**Result:** ✅ SUCCESS (compose file: docker-compose.yml)

- Compose version attribute warning (obsolete) - harmless
- All services parsed correctly
- Network/volume config valid

---

## 2) Services in Compose vs Repo

| Service | Compose Context | Exists? | Dockerfile? |
|---------|----------------|---------|-------------|
| postgres | (image) | ✅ | N/A |
| redis | (image) | ✅ | N/A |
| registry | ./services/registry | ✅ | ❌ **MISSING** |
| payment | ./services/payment | ✅ | ❌ **MISSING** |
| worker | ./services/worker | ✅ | ❌ **MISSING** |
| telegram-bot | ./services/telegram-bot | ❌ **MISSING** | ❌ N/A |
| dashboard | ./services/dashboard | ❌ **MISSING** | ❌ N/A |
| jaeger | (image) | ✅ | N/A |

**Critical Finding:** Dockerfiles missing for all 3 Python services (registry, payment, worker).

---

## 3) Source Files Verified

### Registry (port 8000)
- ✅ `services/registry/app/main.py` — entry point, port 8000
- ✅ `services/registry/app/models.py` — defines all tables
- ✅ `services/registry/app/database.py` — SQLAlchemy setup
- ✅ `services/registry/app/auth.py` — password/token logic
- ✅ `services/registry/app/schemas.py` — Pydantic models
- ✅ `services/registry/app/api/routes/` — auth, agents, tasks, websocket
- ✅ `services/registry/requirements.txt` — 20 deps
- ✅ `services/registry/init-db/01-init.sql` — schema + triggers

### Payment (port 8001)
- ✅ `services/payment/app/main.py` — entry point, port 8001
- ✅ `services/payment/app/models.py` — has its own models
- ✅ `services/payment/app/database.py` — SQLAlchemy setup
- ✅ `services/payment/app/api/routes/` — wallets, transactions, approvals
- ✅ `services/payment/requirements.txt` — 19 deps

### Worker (no port)
- ✅ `services/worker/app/worker.py` — entry point
- ✅ `services/worker/app/models.py` — has its own models
- ✅ `services/worker/requirements.txt` — 10 deps

### Missing Services
- ❌ `services/telegram-bot/` — not in repo
- ❌ `services/dashboard/` — not in repo

---

## 4) Environment

- ✅ `.env.example` exists
- ❌ `.env` (correctly not committed)
- All required env vars documented in `.env.example`

---

## 5) Testing

- ✅ Tests now exist: `tests/test_money_invariants.py`
- ✅ pytest.ini exists

---

## Commands Verified

### Docker (works)
```bash
docker compose config                                    # ✅ passes
docker compose up -d --build                            # will FAIL (no Dockerfiles)
```

### Local (not fully verified - needs Dockerfiles)
```bash
# Registry
cd services/registry && pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Payment
cd services/payment && pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload

# Worker
cd services/worker && pip install -r requirements.txt
python -m app.worker
```

### Testing
```bash
pytest tests/test_money_invariants.py -v  # ✅ 6/6 passed
```

---

## Issues Found

1. **P0:** Missing Dockerfiles for registry, payment, worker
2. **P0:** Missing services: telegram-bot, dashboard (compose references but repo missing)
3. **P1:** No tests exist → **FIXED** ✅ Created tests/test_money_invariants.py
4. **P2:** Need to verify payment/worker import models from registry correctly

---

## MONEY INVARIANT ANALYSIS (P0) — ✅ VERIFIED

### No double wallet update

**DB Triggers (source of truth for balance_*/daily_spent):**
- `update_wallet_balances_trigger`: AFTER UPDATE on transactions WHERE status = 'completed'
  - Updates `balance_credits` / `balance_usdc` (transfer)
- `update_wallet_daily_spent`: AFTER UPDATE on transactions WHERE status = 'completed'
  - Updates `daily_spent`

**Application Code Paths:**
| Path | Action | Risk |
|------|--------|------|
| `tasks.py:create_task_session` | reserves funds (`reserved_credits +=`) | ✅ safe |
| `tasks.py:confirm_task` | releases reserve + transaction COMPLETED → trigger fires | ✅ safe |
| `tasks.py:fail_task` | releases reserve + transaction CANCELLED → trigger does NOT fire | ✅ safe |
| `worker.py` | releases reserve + transaction CANCELLED → trigger does NOT fire | ✅ safe |
| `payment/transactions.py:confirm` | transaction COMPLETED → trigger fires | ✅ safe |

**Invariant:**
- App code ONLY modifies `reserved_credits/reserved_usdc`
- DB triggers ONLY modify `balance_credits/balance_usdc` on COMPLETED
- Trigger NEVER fires on CANCELLED (only on COMPLETED)

### Tests Created
- `tests/test_money_invariants.py` - 6 tests covering:
  1. Transaction completion updates balance exactly once
  2. Refund restores reserved funds correctly
  3. No double-credit scenario
  4. Spending cap enforcement
- `pytest.ini` - test configuration
- **pytest run:** ✅ 6/6 passed

---

## Verified by Running

- `docker compose config` → ✅ SUCCESS
- `pytest tests/test_money_invariants.py -v` → ✅ 6/6 PASSED
- Glob patterns for .py files → ✅ found source files for all services

---

## Follow-up Actions

1. Create Dockerfiles for 3 services (registry, payment, worker)
2. OR remove telegram-bot/dashboard from compose if not intended
3. ~~Add pytest config + basic tests for escrow path~~ → DONE ✅

---

## 2026-03-06 - Full Verification

### 1. docker compose config
```
✅ docker compose config - PASSED
```

### 2. docker compose up -d --build
```
❌ SKIPPED - Docker Desktop not running
Error: Cannot connect to Docker daemon
```

### 3. docker compose ps
```
⏳ PENDING - Requires Docker Desktop
```

### 4. pytest -q
```
✅ 84 passed in 0.63s
```

### 5. python examples/demo_end_to_end.py
```
⏳ PENDING - Requires Docker services running
```

---

## Issues Found

- Docker Desktop not running - cannot verify docker compose up
