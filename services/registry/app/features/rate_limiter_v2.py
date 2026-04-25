"""Rate Limiter v2: ASGI3-compatible rate limiting middleware."""
import time
from collections import defaultdict

class ASGIRateLimiter:
    def __init__(self, rpm=100):
        self.rpm = rpm
        self.requests = defaultdict(list)
    
    async def __call__(self, scope, receive, send):
        client = scope.get("client", ("unknown", 0))[0]
        now = time.time()
        self.requests[client] = [t for t in self.requests[client] if t > now - 60]
        if len(self.requests[client]) >= self.rpm:
            await send({"type": "http.response.start", "status": 429, "headers": [
                (b"content-type", b"application/json")
            ]})
            await send({"type": "http.response.body", "body": b'{"detail":"Rate limit exceeded"}'})
            return
        self.requests[client].append(now)
