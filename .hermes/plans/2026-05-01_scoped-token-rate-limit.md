# Scoped Token Enforcement + Rate Limiting — Implementation Plan

> **For Hermes:** Implement task-by-task. 5-8 phút mỗi task. Commit sau mỗi task.

**Goal:** Wire up 2 security features đã code nhưng chưa deployed: (A) Scoped token (`spt_xxx`) enforcement trong auth layer, (B) Rate limiting middleware.

**Architecture:**
- (A) Thêm ~20 dòng vào `verify_token()` trong `auth.py` để nhận diện `spt_` prefix → hash → DB lookup → return TokenData kèm scope
- (B) Sửa 1 dòng import `aioredis` → `redis.asyncio` trong `rate_limiter.py`, uncomment middleware trong `main.py`

**Tech Stack:** FastAPI, SQLAlchemy, PostgreSQL, Redis, Python 3.11

**Risk Assessment:**
- (A) Zero risk — thêm code path mới, không đụng JWT path. Scoped token không tồn tại thì fall through sang JWT như cũ.
- (B) Low risk — middleware đã code đúng, chỉ cần sửa deprecated import + wire up. Nếu lỗi, comment lại là xong.

**Rollback plan:** Mỗi task commit riêng. Nếu fail → `git revert`.

---

## Phase A: Scoped Token Enforcement

### A-1: Move `_hash_token` from tokens.py to auth.py

**Objective:** `_hash_token` đang nằm trong `api/routes/tokens.py` — auth.py không import được vì circular dependency. Copy function vào auth.py.

**Files:**
- Modify: `services/registry/app/auth.py`

**Step 1: Add _hash_token to auth.py**

Thêm vào cuối file `auth.py` (sau dòng 203):

```python
def _hash_scoped_token(raw: str) -> str:
    """Hash a scoped token using SHA-256 — mirrors tokens.py:_hash_token."""
    return hashlib.sha256(raw.encode()).hexdigest()
```

**Step 2: Verify import**

`hashlib` đã được import ở dòng 2 của auth.py — không cần thêm import.

**Step 3: Commit**

```bash
git add services/registry/app/auth.py
git commit -m "feat(auth): add _hash_scoped_token helper for spt_ token verification"
```

---

### A-2: Add `scoped_token` field to TokenData schema

**Objective:** TokenData hiện chỉ có `user_id` và `agent_id`. Cần thêm optional `scoped_token` để route downstream kiểm tra scope.

**Files:**
- Modify: `services/registry/app/schemas.py:282-284`

**Step 1: Edit TokenData**

```python
class TokenData(BaseModel):
    user_id: Optional[UUIDAny] = None
    agent_id: Optional[UUIDAny] = None
    scoped_token_id: Optional[UUIDAny] = None      # spt_ token ID (if scoped)
    allowed_actions: Optional[list[str]] = None      # scope from scoped token
    spending_cap: Optional[int] = None               # remaining cap
    resource_type: Optional[str] = None              # resource scope
    resource_id: Optional[str] = None                # specific resource
```

**Step 2: Verify**

Không có code nào đọc `TokenData.user_id`/`agent_id` bị ảnh hưởng — field mới là optional, backward compatible.

**Step 3: Commit**

```bash
git add services/registry/app/schemas.py
git commit -m "feat(schemas): add scoped token fields to TokenData"
```

---

### A-3: Add spt_ verification logic to verify_token()

**Objective:** Khi token có prefix `spt_`, hash → DB lookup → return TokenData kèm scope. Token không tồn tại hoặc revoked → 401.

**Files:**
- Modify: `services/registry/app/auth.py:95-120` (verify_token function)

**Step 1: Add import cho ScopedToken model**

Thêm vào import block của auth.py (dòng 18):

```python
from .models import Agent, User, ScopedToken
```

**Step 2: Add import cho datetime timezone**

Thêm vào dòng 6 (sau `from datetime import datetime, timedelta`):

```python
from datetime import datetime, timedelta, timezone
```

**Step 3: Rewrite verify_token — add spt_ check BEFORE JWT decode**

```python
def verify_token(token: str, db: Optional[Session] = None) -> TokenData:
    """Verify a JWT token or scoped token (spt_) and return token data.
    
    Supports two token types:
    - JWT: standard user/agent Bearer tokens
    - spt_: scoped API tokens with resource limits
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # ── Scoped token (spt_ prefix) ──
    if token.startswith("spt_"):
        if db is None:
            raise credentials_exception  # DB session required for spt_ lookup
        token_hash = _hash_scoped_token(token)
        spt = db.query(ScopedToken).filter(
            ScopedToken.token_hash == token_hash,
            ScopedToken.is_revoked == False,
        ).first()
        if not spt:
            raise credentials_exception
        if spt.expires_at and spt.expires_at < datetime.now(timezone.utc):
            raise credentials_exception
        # Update total_spent (optional — track usage)
        return TokenData(
            agent_id=spt.agent_id,
            scoped_token_id=spt.id,
            allowed_actions=spt.allowed_actions or [],
            spending_cap=spt.spending_cap,
            resource_type=spt.resource_type,
            resource_id=spt.resource_id,
        )

    # ── JWT token (existing logic) ──
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        id: str = payload.get("sub")
        token_type: str = payload.get("type")

        if id is None or token_type is None:
            raise credentials_exception

        if token_type == "user":
            token_data = TokenData(user_id=uuid.UUID(id))
        elif token_type == "agent":
            token_data = TokenData(agent_id=uuid.UUID(id))
        else:
            raise credentials_exception

        return token_data
    except (JWTError, ValidationError):
        raise credentials_exception
```

**Step 4: Update get_current_user / get_current_agent / get_current_user_or_agent**

Cả 3 hàm gọi `verify_token(token)` không truyền `db`. Cần thêm `db` param:

```python
async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    token_data = verify_token(token, db=db)
    # ... rest unchanged

async def get_current_agent(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> Agent:
    token_data = verify_token(token, db=db)
    # ... rest unchanged

async def get_current_user_or_agent(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> Union[User, Agent]:
    token_data = verify_token(token, db=db)
    # ... rest unchanged
```

**Step 5: Update WebSocket auth**

File `websocket_manager.py` line 102 gọi `verify_token(token)` — cần pass db session. Nhưng WebSocket không có Depends(get_db). Solution: trong WebSocket context, tạo DB session manually nếu token là spt_.

```python
# In websocket_manager.py (line ~102):
# Before: token_data = verify_token(token)
# After:  token_data = verify_token(token, db=db_session)
```

Thêm `db=db_session` vào call verify_token trong websocket_manager.py.

**Step 6: Commit**

```bash
git add services/registry/app/auth.py services/registry/app/schemas.py services/registry/app/websocket_manager.py
git commit -m "feat(auth): enforce scoped token (spt_) verification in auth layer"
```

---

### A-4: Smoke test scoped token enforcement

**Objective:** Verify spt_ tokens work trong thực tế.

**Step 1: Create a scoped token**

```bash
TOKEN=$(curl -s -X POST 'http://localhost:8000/v1/auth/user/login' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'username=annhien.dev@gmail.com&password=TestPass123' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Get an agent ID
AGENT_ID=$(curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/v1/agents/ | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

# Create a scoped token
SPT=$(curl -s -X POST "http://localhost:8000/v1/tokens" \
  -H "Content-Type: application/json" \
  -d "{\"agent_id\":\"$AGENT_ID\",\"resource_type\":\"project\",\"spending_cap\":500,\"allowed_actions\":[\"read\"]}" | python3 -c "import sys,json; print(json.load(sys.stdin)['raw_token'])")
echo "Scoped token: $SPT"
```

**Step 2: Test with scoped token**

```bash
# Should return 200
curl -s -o /dev/null -w "HTTP %{http_code}" -H "Authorization: Bearer $SPT" http://localhost:8000/v1/agents/
echo " (expected: 200)"

# Test with fake spt_ token
curl -s -o /dev/null -w "HTTP %{http_code}" -H "Authorization: Bearer spt_fake_invalid_token" http://localhost:8000/v1/agents/
echo " (expected: 401)"
```

**Step 3: Commit (if needed — test-only)**

---

### A-5: Rebuild registry and verify in production

```bash
cd /opt/agentnet
docker compose -f docker-compose.prod.yml up -d --build registry
sleep 5
curl -s http://localhost:8000/health
```

---

## Phase B: Rate Limiting

### B-1: Fix deprecated aioredis import in rate_limiter.py

**Objective:** `aioredis` 2.0.1 deprecated. API `aioredis.from_url()` → `redis.asyncio.from_url()`.

**Files:**
- Modify: `services/registry/app/api/rate_limiter.py:13,61`

**Step 1: Change import**

```python
# Before (line 13):
import aioredis

# After:
import redis.asyncio as aioredis
```

**Step 2: Verify from_url API**

`redis.asyncio.from_url(url)` exists in redis 5.0.1 (confirmed installed). Same signature, no code changes needed.

**Step 3: Commit**

```bash
git add services/registry/app/api/rate_limiter.py
git commit -m "fix(rate-limit): replace deprecated aioredis with redis.asyncio"
```

---

### B-2: Wire up RateLimitMiddleware in main.py

**Objective:** Uncomment middleware. Add env-based config.

**Files:**
- Modify: `services/registry/app/main.py:45`

**Step 1: Replace commented line**

```python
# Before (line 43-45):
# Mount rate limiter (60 req/min/IP default)
# Disabled — middleware needs ASGI-compatible refactor
# # app.add_middleware(RateLimitMiddleware, ...)

# After:
# Mount rate limiter (env-configurable, defaults 100 req/min for users, 300 for agents)
import os
app.add_middleware(
    RateLimitMiddleware,
    default_rate=int(os.getenv("RATE_LIMIT_USER_PER_MIN", "100")),
    default_burst=int(os.getenv("RATE_LIMIT_USER_BURST", "150")),
    agent_rate=int(os.getenv("RATE_LIMIT_AGENT_PER_MIN", "300")),
    agent_burst=int(os.getenv("RATE_LIMIT_AGENT_BURST", "450")),
    redis_url=os.getenv("REDIS_URL", None),  # None = in-memory mode
)
```

**Step 2: Commit**

```bash
git add services/registry/app/main.py
git commit -m "feat(rate-limit): wire up RateLimitMiddleware with env-based config"
```

---

### B-3: Smoke test rate limiting

**Objective:** Send 120 rapid requests, verify 429 xuất hiện sau ~100.

```bash
# Send 120 requests to /v1/agents/public/ (public endpoint, no auth)
for i in $(seq 1 120); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/v1/agents/public/)
  echo "$i: $code"
  if [ "$code" = "429" ]; then
    echo "RATE LIMITED at request $i!"
    break
  fi
done
```

Expected: sau ~100 requests → HTTP 429 + `X-RateLimit-Remaining: 0` + `Retry-After`.

---

### B-4: Rebuild registry and verify in production

```bash
cd /opt/agentnet
docker compose -f docker-compose.prod.yml up -d --build registry
sleep 5
# Quick rate limit check
for i in $(seq 1 110); do curl -s -o /dev/null -w '%{http_code} ' http://localhost:8000/health; done | grep -o '429' | head -1
# Should see 429 if rate limiting works (health is skip_path, so change to different endpoint)
```

**Note:** Health check IS in skip_paths list (dòng 87 rate_limiter.py) — dùng `/v1/agents/public/` để test thay.

---

## Execution Order

```
A-1 → A-2 → A-3 → A-4 → A-5 → B-1 → B-2 → B-3 → B-4
```

Mỗi task: implement → test → commit → push. Tổng thời gian dự kiến: 45-60 phút.

## Post-Deployment Verification

Sau khi deploy cả 2, chạy:

```bash
# 1. Health check
curl -s http://localhost:8000/health

# 2. Scoped token create + use
curl -s -X POST .../v1/tokens ... → 201
curl -s -H "Authorization: Bearer spt_xxx" .../v1/agents/ → 200

# 3. Rate limit test
for i in $(seq 1 110); do curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/v1/agents/public/; done | sort | uniq -c
# Must include 429

# 4. JWT auth still works (regression)
curl -s -H "Authorization: Bearer <valid_jwt>" .../v1/agents/ → 200

# 5. Fake spt_ rejected
curl -s -H "Authorization: Bearer spt_fake" .../v1/agents/ → 401
```

## Edge Cases & Risks

| Case | Mitigation |
|------|-----------|
| `spt_` token đã revoked | DB filter `is_revoked == False` |
| `spt_` token hết hạn | Check `expires_at < now` |
| DB session null trong verify_token | db param optional — `if db is None: raise` |
| WebSocket không có Depends | Manual DB session creation |
| RateLimitMiddleware crash | Comment lại trong main.py — zero impact |
| Redis không available | Fallback to in-memory TokenBucket |
| `redis.asyncio` không có `from_url` | Đã verify — redis 5.0.1 có API này |
