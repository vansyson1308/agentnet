"""
Health, readiness, Prometheus metrics for the simulation service.

Mirrors ``services/registry/app/health.py``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import redis.asyncio as redis
from fastapi import APIRouter, FastAPI, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Histogram,
    generate_latest,
)
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from .config import REDIS_URL
from .database import engine


def _counter(name: str, doc: str, labels: list[str]) -> Counter:
    existing = getattr(REGISTRY, "_names_to_collectors", {}).get(name)
    if existing is not None:
        return existing
    return Counter(name, doc, labels)


def _histogram(name: str, doc: str, labels: list[str]) -> Histogram:
    existing = getattr(REGISTRY, "_names_to_collectors", {}).get(name)
    if existing is not None:
        return existing
    return Histogram(name, doc, labels)


http_requests_total = _counter(
    "agentnet_http_requests_total",
    "Total HTTP requests handled.",
    ["service", "method", "path", "status"],
)
http_request_duration_seconds = _histogram(
    "agentnet_http_request_duration_seconds",
    "Latency of HTTP requests.",
    ["service", "method", "path"],
)
simulation_runs_total = _counter(
    "agentnet_simulation_runs_total",
    "Simulations by terminal state.",
    ["status"],
)


def make_metrics_middleware(service_name: str):
    class MetricsMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.url.path == "/metrics":
                return await call_next(request)
            start = time.perf_counter()
            response = await call_next(request)
            duration = time.perf_counter() - start
            route = request.scope.get("route")
            path_label = getattr(route, "path", request.url.path) if route else request.url.path
            http_requests_total.labels(
                service=service_name,
                method=request.method,
                path=path_label,
                status=str(response.status_code),
            ).inc()
            http_request_duration_seconds.labels(
                service=service_name,
                method=request.method,
                path=path_label,
            ).observe(duration)
            return response

    return MetricsMiddleware


def make_health_router(service_name: str) -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "service": service_name}

    @router.get("/readyz")
    async def readyz() -> Response:
        errs: list[str] = []
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as e:
            errs.append(f"db: {e}")
        client: Optional[redis.Redis] = None
        try:
            client = redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
            await asyncio.wait_for(client.ping(), timeout=2.0)
        except Exception as e:
            errs.append(f"redis: {e}")
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    pass
        if errs:
            return Response(
                content='{"status":"not_ready","errors":' + str(errs) + "}",
                status_code=503,
                media_type="application/json",
            )
        return Response(
            content='{"status":"ready","service":"' + service_name + '"}',
            status_code=200,
            media_type="application/json",
        )

    @router.get("/metrics")
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return router


def install_health_and_metrics(app: FastAPI, service_name: str) -> None:
    app.add_middleware(make_metrics_middleware(service_name))
    app.include_router(make_health_router(service_name))
