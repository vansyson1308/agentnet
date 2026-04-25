import time
from collections import defaultdict

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory token bucket rate limiter per IP."""

    def __init__(self, app, max_requests: int = 60, window_seconds: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.records = defaultdict(lambda: {"count": 0, "reset_time": 0.0})

    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host if request.client else "unknown"
        record = self.records[client_ip]
        now = time.time()

        if now > record["reset_time"] + self.window_seconds:
            record["count"] = 0
            record["reset_time"] = now

        record["count"] += 1

        if record["count"] > self.max_requests:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please try again later."},
            )

        response = await call_next(request)
        return response