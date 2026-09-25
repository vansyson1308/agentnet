"""
Rate Limiter — Redis-based token bucket middleware
Dùng token bucket algorithm với Redis để rate limit requests.
Config: rate_limiter.py
"""
import asyncio
import time
import hashlib
from typing import Optional, Callable, Any
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
import redis.asyncio as aioredis
from jose import JWTError, jwt

from ..config import JWT_ALGORITHM, JWT_SECRET_KEY

class TokenBucket:
    """In-memory token bucket fallback (khi Redis chưa available)."""
    def __init__(self, rate: int, burst: int):
        self.rate = rate  # tokens/second
        self.burst = burst  # max tokens
        self.tokens = burst
        self.last_refill = time.time()

    def consume(self) -> bool:
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
        self.last_refill = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Rate limiting middleware cho FastAPI.
    Dùng token bucket per-client, support Redis nếu có.
    """
    
    def __init__(
        self,
        app: ASGIApp,
        default_rate: int = 100,       # requests per minute
        default_burst: int = 150,      # burst limit
        agent_rate: int = 300,         # requests per minute for agents
        agent_burst: int = 450,        # burst limit for agents
        redis_url: Optional[str] = None,
    ):
        super().__init__(app)
        self.default_rate = default_rate
        self.default_burst = default_burst
        self.agent_rate = agent_rate
        self.agent_burst = agent_burst
        self.redis_url = redis_url
        self._buckets: dict[str, TokenBucket] = {}
        self._redis = None
        # Serialise concurrent first-call init so we don't spawn N redis
        # connections in the few-ms window before the first one resolves.
        self._redis_init_lock: Optional[asyncio.Lock] = None

    async def _get_redis(self):
        if self._redis is not None or not self.redis_url:
            return self._redis
        if self._redis_init_lock is None:
            self._redis_init_lock = asyncio.Lock()
        async with self._redis_init_lock:
            if self._redis is not None:
                return self._redis
            try:
                self._redis = await asyncio.wait_for(
                    aioredis.from_url(self.redis_url), timeout=5.0
                )
            except Exception:
                self._redis = None
        return self._redis

    @staticmethod
    def _verified_identity(request: Request) -> Optional[tuple[str, str]]:
        """``(bucket_key, token_type)`` for a bearer JWT this service itself
        signed and that has not expired; ``None`` for anything else.

        Only a VERIFIED token may name a bucket. Keying on the raw header let
        any caller mint a fresh bucket per request by sending
        ``Authorization: Bearer <random>`` (measured on the public edge: each
        garbage token got its own 300/min agent-tier bucket), which is a
        rate-limit bypass on login/register. A forged, expired or unverifiable
        token (including ``spt_`` scoped tokens, which need a database lookup
        this middleware does not do) falls back to the peer address."""
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[len("Bearer "):].strip()
        if not token or token.startswith("spt_"):
            return None
        try:
            payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        except (JWTError, ValueError):
            return None
        subject, token_type = payload.get("sub"), payload.get("type")
        if not subject or token_type not in ("user", "agent"):
            return None
        digest = hashlib.sha256(f"{token_type}:{subject}".encode()).hexdigest()[:16]
        return f"sub:{digest}", token_type

    def _get_client_key(self, request: Request) -> str:
        """Unique key per caller: the verified token subject if there is one,
        otherwise the peer address as the trusted edge reports it.

        We deliberately do NOT read X-Forwarded-For here. Any client can
        send that header, so parsing it in the application would let an
        unauthenticated caller pick a fresh bucket per request (rate-limit
        bypass on login/register). The peer address comes from
        ``request.client``, which only the trusted edge middleware
        (``proxy_headers.py``) or uvicorn's ``--proxy-headers`` may rewrite,
        so the trust decision lives in one place, configured per deployment.
        An unverified bearer token is treated exactly like no token."""
        identity = self._verified_identity(request)
        if identity is not None:
            return identity[0]
        return (request.client.host if request.client else None) or "unknown"

    async def _is_agent(self, request: Request) -> bool:
        """The agent tier requires a verified agent token. A caller cannot
        claim the higher agent rate by sending an arbitrary short token."""
        identity = self._verified_identity(request)
        return identity is not None and identity[1] == "agent"

    async def _redis_consume(self, client_key: str, rate: int) -> tuple[bool, int]:
        """Fixed-window per-minute counter via Redis INCR + EX.

        Returns ``(allowed, remaining)``. Slightly stricter than a token
        bucket — every minute boundary fully resets the count — but that's
        the simplest correct behaviour across multiple replicas without
        needing a Lua script. Falls back to caller's in-memory bucket if
        Redis is unreachable.
        """
        client = await self._get_redis()
        if client is None:
            return True, -1  # signal "no redis, do in-memory"
        bucket = int(time.time() // 60)
        key = f"agentnet:rl:{client_key}:{bucket}"
        try:
            count = await client.incr(key)
            if count == 1:
                await client.expire(key, 70)  # slack for clock skew
            if count > rate:
                return False, 0
            return True, max(0, rate - count)
        except Exception:
            # Redis hiccup — fail open and let the in-memory bucket take over.
            return True, -1

    async def dispatch(self, request: Request, call_next: Callable):
        # Skip rate limiting for health/metrics/docs.
        skip_paths = [
            "/v1/health",
            "/healthz",
            "/readyz",
            "/metrics",
            "/docs",
            "/openapi.json",
            "/v1/stats",
        ]
        if any(request.url.path.startswith(p) for p in skip_paths):
            return await call_next(request)

        client_key = self._get_client_key(request)
        is_agent = await self._is_agent(request)

        rate = self.agent_rate if is_agent else self.default_rate
        burst = self.agent_burst if is_agent else self.default_burst

        # Try Redis first (correct under multi-replica). If Redis isn't
        # configured or is unreachable, fall back to in-memory token bucket.
        allowed, remaining = await self._redis_consume(client_key, rate)
        if remaining < 0:  # signal: redis unavailable
            rate_per_sec = rate / 60.0
            if client_key not in self._buckets:
                self._buckets[client_key] = TokenBucket(rate_per_sec, burst)
            bucket = self._buckets[client_key]
            allowed = bucket.consume()
            remaining = max(0, int(bucket.tokens))

        if not allowed:
            retry_after = int(60.0 / rate) if rate > 0 else 60
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Too Many Requests",
                    "retry_after_seconds": retry_after,
                    "message": f"Rate limit exceeded. Max {rate} requests/minute.",
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(rate),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(time.time() + retry_after)),
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(rate)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Burst"] = str(burst)
        return response


def add_rate_limiter(app: FastAPI):
    """Helper function to add rate limiter from main.py"""
    app.add_middleware(RateLimitMiddleware, default_rate=60, default_burst=120)
    return None
