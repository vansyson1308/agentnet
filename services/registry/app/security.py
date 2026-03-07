"""
Security middleware and utilities.

Provides:
- Environment-based CORS configuration
- Rate limiting for FastAPI endpoints
- Security headers
"""

import os
import time
from collections import defaultdict
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# ============================================================
# CORS Configuration
# ============================================================


def get_cors_origins() -> List[str]:
    """
    Get CORS origins based on environment.

    Development: allow localhost origins only
    Production: require explicit allowlist from env var
    """
    env = os.getenv("ENVIRONMENT", "development").lower()

    if env == "development":
        return [
            "http://localhost:3000",
            "http://localhost:8000",
            "http://localhost:8001",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:8000",
            "http://127.0.0.1:8001",
        ]
    else:
        # Production: require explicit allowlist
        allowed = os.getenv("CORS_ALLOWED_ORIGINS", "")
        if not allowed:
            raise ValueError("CORS_ALLOWED_ORIGINS must be set in non-development environments")
        return [origin.strip() for origin in allowed.split(",") if origin.strip()]


def setup_cors(app: FastAPI):
    """Setup CORS middleware with environment-based config."""
    try:
        allowed_origins = get_cors_origins()
    except ValueError as e:
        # In production without config, deny all
        if os.getenv("ENVIRONMENT", "").lower() != "development":
            allowed_origins = []
        else:
            allowed_origins = ["http://localhost:3000"]  # Fallback for dev

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )


# ============================================================
# Rate Limiting
# ============================================================


class InMemoryRateLimiter:
    """
    Simple in-memory rate limiter for development.

    For production, use Redis-backed rate limiting.
    """

    def __init__(self, requests_per_minute: int = 60):
        self.requests_per_minute = requests_per_minute
        self.requests: dict = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        """Check if request is allowed for given key."""
        now = time.time()
        minute_ago = now - 60

        # Clean old requests
        self.requests[key] = [t for t in self.requests[key] if t > minute_ago]

        if len(self.requests[key]) >= self.requests_per_minute:
            return False

        self.requests[key].append(now)
        return True

    def get_remaining(self, key: str) -> int:
        """Get remaining requests for key."""
        now = time.time()
        minute_ago = now - 60
        self.requests[key] = [t for t in self.requests[key] if t > minute_ago]
        return max(0, self.requests_per_minute - len(self.requests[key]))


# Global rate limiter instance
_rate_limiter: Optional[InMemoryRateLimiter] = None


def get_rate_limiter() -> InMemoryRateLimiter:
    """Get or create rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        limit = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
        _rate_limiter = InMemoryRateLimiter(requests_per_minute=limit)
    return _rate_limiter


async def check_rate_limit(key: str) -> bool:
    """Check if request is allowed. Raises HTTPException if not."""
    limiter = get_rate_limiter()
    if not limiter.is_allowed(key):
        remaining = limiter.get_remaining(key)
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Try again later.",
            headers={"X-RateLimit-Remaining": str(remaining)},
        )


# ============================================================
# Security Headers Middleware
# ============================================================


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)

        # Basic security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        return response


def setup_security_headers(app: FastAPI):
    """Setup security headers middleware."""
    app.add_middleware(SecurityHeadersMiddleware)


# ============================================================
# Configuration
# ============================================================


def is_development() -> bool:
    """Check if running in development mode."""
    return os.getenv("ENVIRONMENT", "development").lower() == "development"
