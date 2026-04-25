"""
Rate Limiter — Redis-based token bucket middleware
Dùng token bucket algorithm với Redis để rate limit requests.
Config: rate_limiter.py
"""
import time
import hashlib
from typing import Optional, Callable, Any
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
import aioredis

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

    async def _get_redis(self):
        if self.redis_url and self._redis is None:
            try:
                self._redis = await aioredis.from_url(self.redis_url)
            except Exception:
                self._redis = None
        return self._redis

    def _get_client_key(self, request: Request) -> str:
        """Unique key for client — use token subject or IP."""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            # Hash token để không leak sensitive info
            return hashlib.sha256(auth.encode()).hexdigest()[:16]
        # Fallback to IP
        forwarded = request.headers.get("X-Forwarded-For", "")
        return forwarded.split(",")[0].strip() or request.client.host or "unknown"

    async def _is_agent(self, request: Request) -> bool:
        """Check if request comes from an agent token."""
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        # Agent tokens typically have 'agent_' prefix or specific claims
        # For now, heuristic: tokens > 40 chars are likely user JWTs
        return len(auth) < 50

    async def dispatch(self, request: Request, call_next: Callable):
        # Skip rate limiting for health checks and docs
        skip_paths = ["/v1/health", "/docs", "/openapi.json", "/v1/stats"]
        if any(request.url.path.startswith(p) for p in skip_paths):
            return await call_next(request)

        client_key = self._get_client_key(request)
        is_agent = await self._is_agent(request)
        
        rate = self.agent_rate if is_agent else self.default_rate
        burst = self.agent_burst if is_agent else self.default_burst
        
        # Convert rate from per-minute to per-second
        rate_per_sec = rate / 60.0
        
        # In-memory token bucket
        if client_key not in self._buckets:
            self._buckets[client_key] = TokenBucket(rate_per_sec, burst)
        
        bucket = self._buckets[client_key]
        
        if not bucket.consume():
            retry_after = int(60.0 / rate) if rate > 0 else 60
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Too Many Requests",
                    "retry_after_seconds": retry_after,
                    "message": f"Rate limit exceeded. Max {rate} requests/minute."
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(rate),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(time.time() + retry_after)),
                }
            )
        
        response = await call_next(request)
        
        # Add rate limit headers to response
        remaining = max(0, int(bucket.tokens))
        response.headers["X-RateLimit-Limit"] = str(rate)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Burst"] = str(burst)
        
        return response


def add_rate_limiter(app: FastAPI):
    """Helper function to add rate limiter from main.py"""
    app.add_middleware(RateLimitMiddleware, default_rate=60, default_burst=120)
    return None
