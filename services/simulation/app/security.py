"""
Security middleware for the simulation service.
Reuses the same patterns as the registry service.
"""

import os
from typing import List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


def get_cors_origins() -> List[str]:
    env = os.getenv("ENVIRONMENT", "development").lower()
    if env == "development":
        return [
            "http://localhost:3000",
            "http://localhost:8000",
            "http://localhost:8001",
            "http://localhost:8002",
            "http://localhost:8080",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:8002",
        ]
    else:
        allowed = os.getenv("CORS_ALLOWED_ORIGINS", "")
        if not allowed:
            return []
        return [origin.strip() for origin in allowed.split(",") if origin.strip()]


def setup_cors(app: FastAPI):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_cors_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


def setup_security_headers(app: FastAPI):
    app.add_middleware(SecurityHeadersMiddleware)
