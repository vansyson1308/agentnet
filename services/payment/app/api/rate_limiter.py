import asyncio
import time
from collections import defaultdict
from typing import Optional

import redis.asyncio as aioredis
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP fixed-window rate limiter, Redis-backed in production.

    Falls back to an in-memory counter if no ``redis_url`` is configured
    or if Redis is unreachable. The fallback is correct for a single
    replica; in a multi-replica deployment Redis is required so the
    limit is shared across processes.
    """

    def __init__(
        self,
        app,
        max_requests: int = 60,
        window_seconds: int = 60,
        redis_url: Optional[str] = None,
    ):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.redis_url = redis_url
        self._redis: Optional[aioredis.Redis] = None
        self._redis_init_lock: Optional[asyncio.Lock] = None
        self.records: dict = defaultdict(lambda: {"count": 0, "reset_time": 0.0})

    async def _get_redis(self) -> Optional[aioredis.Redis]:
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

    async def _try_redis_consume(self, ip: str) -> Optional[bool]:
        client = await self._get_redis()
        if client is None:
            return None
        bucket = int(time.time() // self.window_seconds)
        key = f"agentnet:rl:payment:{ip}:{bucket}"
        try:
            count = await client.incr(key)
            if count == 1:
                await client.expire(key, self.window_seconds + 10)
            return count <= self.max_requests
        except Exception:
            return None

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for health/metrics so probes never get 429.
        if request.url.path in ("/healthz", "/readyz", "/metrics", "/health"):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"

        allowed = await self._try_redis_consume(client_ip)
        if allowed is None:
            # Redis unavailable — in-memory fallback.
            record = self.records[client_ip]
            now = time.time()
            if now > record["reset_time"] + self.window_seconds:
                record["count"] = 0
                record["reset_time"] = now
            record["count"] += 1
            allowed = record["count"] <= self.max_requests

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please try again later."},
            )
        return await call_next(request)
