# AgentNet World-Class QA Audit — Part B4: Security Pentest Report
# Date: May 1, 2026
# Status: COMPLETE

---

## B4-1: Token Scope Enforcement — ❌ FAIL

### Finding: Scoped tokens (spt_xxx) are created but NEVER enforced

**Evidence:**
- `auth.py` verify_token() only handles JWT tokens (types: "user", "agent"). No code path for spt_ prefix
- `auth.py` does NOT import ScopedToken model or _hash_token function
- No middleware validates spt_ tokens before JWT
- WebSocket manager also uses verify_token() — JWT-only
- curl tests: `spt_fake_test_token` → 401 "Could not validate credentials" (identical to garbage JWT)
- DB has 4 active scoped tokens with allowed_actions, spending_cap — zero effect on auth

**Root cause:** ScopedToken exists in DB + creation API, but auth layer was never updated to recognize them.

**Severity:** HIGH — entire APP protocol's security model (scoped tokens for granular access) is non-functional.

---

## B4-2: Rate Limiting — ❌ FAIL

### Finding: Zero rate limiting at any layer

**Evidence:**
- 75 rapid requests to /health, /, /v1/catalog/services, /v1/agents/public/ → ALL 200, ZERO 429
- No X-RateLimit-* or Retry-After headers on any response
- RateLimitMiddleware commented out in main.py line 45-46: "# # app.add_middleware(RateLimitMiddleware, ...)"
- `add_rate_limiter()` imported but never called
- No route uses `Depends(check_rate_limit)`
- Nginx: no limit_req_zone/limit_req directives for AgentNet block
- iptables: standard port rules only, no connection-rate limits
- fail2ban: only sshd jail, no HTTP/API jails

**Root cause:** Code exists (InMemoryRateLimiter, RateLimitMiddleware) but entirely disconnected.

**Severity:** HIGH — API is wide open to brute force, DoS, and scraping.

---

## B4-3: Input Validation & Injection — ✅ PASS (with notes)

### SQL Injection: ALL BLOCKED (6/6)
- Pydantic v2 UUID validation rejects non-UUID path params at type level
- Email validator rejects injection characters
- Login credential check uses parameterized queries — no raw SQL
- URL query params treated as plain strings by ORM

### XSS: ALL BLOCKED (5/5)
- API is pure JSON (application/json) — no HTML rendering surface
- Security headers: X-XSS-Protection, X-Content-Type-Options, X-Frame-Options, HSTS
- No reflected parameters in response bodies
- Header injection attempts: no reflection

### Input Validation: STRONG
- Pydantic v2 provides ge/le bounds (limit >= 1, limit <= 1000)
- Type-level protection on UUID, email, integer fields
- 10KB string and null bytes in search param: no crash, graceful handling
- Wrong Content-Type: properly rejected with validation errors

### Error Disclosure: GOOD
- No stack traces, no internal paths, no DB schema in errors
- Clean JSON: {"detail": "Not Found"}, {"detail": "Method Not Allowed"}
- Pydantic validation errors are verbose but standard for FastAPI dev mode

### ⚠️ INFO DISCLOSURE — MEDIUM
- `/docs` (Swagger UI) publicly exposed on both registry:8000 and payment:8001
- `/openapi.json` exposes full API blueprint (40+ endpoints, schemas, enum values)
- Recommendation: restrict to authenticated admin or disable in production

**Overall:** Strong Pydantic v2 protection. SQLi and XSS fully mitigated. Only concern is OpenAPI exposure.

---

## B4-4: Auth Bypass — ❌ FAIL (Critical findings)

### CRITICAL — Unauthenticated endpoints that should require auth:

| Endpoint | Method | Finding |
|----------|--------|---------|
| `/v1/orchestrator/partners` | GET | Lists all platform partners + client_ids — no auth |
| `/v1/orchestrator/partners` | POST | Register as orchestrator partner — no auth |
| `/v1/projects` | GET | Lists all projects — no auth |
| `/v1/projects` | POST | Create projects — no auth |
| `/v1/tokens` | POST | Create scoped tokens — no auth (422 body validation, but no auth gate) |

### CHAINED ATTACK DEMONSTRATED:
```
POST /v1/orchestrator/partners (no auth)
  → Returns client_id + client_secret
  → POST /v1/orchestrator/provision with those creds
    → Creates user + agent + wallet (100 credits) + project + scoped token
    → FULL ACCOUNT TAKEOVER without any authentication
POST /v1/projects (no auth)
  → Creates arbitrary projects under any agent
```

### SECURE endpoints (verified):
All task/agent/wallet/goal/memory write endpoints require auth and return 401.

### INTENTIONALLY PUBLIC (verified against source):
- GET /v1/goals/ — docstring: "Public"
- GET /v1/memory/ — docstring: "Public"
- GET /v1/fleet/activity — docstring: "No auth required"
- GET /v1/stats — no auth dependency
- GET /v1/agents/public/ — intentional public endpoint
- POST /v1/agents/public-register — intentional public endpoint

**Severity:** CRITICAL — orchestrator partner + project creation without auth enables full account takeover.

---

## Summary

| Test | Result | Severity |
|------|--------|----------|
| B4-1 Token Scope Enforcement | ❌ FAIL | HIGH |
| B4-2 Rate Limiting | ❌ FAIL | HIGH |
| B4-3 Input Validation | ✅ PASS | — |
| B4-3 SQL Injection | ✅ ALL BLOCKED | — |
| B4-3 XSS | ✅ ALL BLOCKED | — |
| B4-3 OpenAPI Exposure | ⚠️ INFO DISCLOSURE | MEDIUM |
| B4-4 Auth Bypass (orchestrator) | ❌ FAIL | CRITICAL |
| B4-4 Auth Bypass (projects) | ❌ FAIL | CRITICAL |
| B4-4 Auth Bypass (tokens) | ❌ FAIL | HIGH |
| B4-4 Protected Endpoints | ✅ SECURE | — |

### Top 5 Fixes Needed (sorted by severity):

1. **CRITICAL**: Add `Depends(get_current_user)` to POST/GET /v1/orchestrator/partners
2. **CRITICAL**: Add `Depends(get_current_user)` to POST/GET /v1/projects
3. **HIGH**: Implement scoped token verification in auth.py — allow spt_ tokens as Bearer auth
4. **HIGH**: Wire up rate limiter middleware in main.py (uncomment + refactor for ASGI)
5. **MEDIUM**: Restrict /docs and /openapi.json in production
