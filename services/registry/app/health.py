"""
Health, readiness, and Prometheus metrics endpoints.

Three endpoints, mounted at the root of the app:

* ``GET /healthz`` — liveness probe. Returns 200 immediately. Does NOT
  touch the DB; if uvicorn can answer, the process is alive. Use this
  for Kubernetes ``livenessProbe`` so a slow DB doesn't cause a kill.
* ``GET /readyz`` — readiness probe. Pings Postgres (``SELECT 1``) and
  Redis (``PING``). Returns 200 only when both are reachable. Use this
  for Kubernetes ``readinessProbe`` so traffic isn't routed in until
  the service can actually serve a request.
* ``GET /metrics`` — Prometheus text-format metrics. Counters for HTTP
  requests, escrow lifecycle, and span persistence failures.

Metrics are global, single-process — fine for the registry / payment /
simulation services since each replica scrapes its own ``/metrics``.
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
    Gauge,
    Histogram,
    generate_latest,
)
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from .config import REDIS_URL
from .database import engine

# ---------------------------------------------------------------------------
# Metrics — module-level singletons so middleware and route handlers share
# them. We guard each registration so two services in the same Python
# process (e.g. tests importing both registry + payment health modules)
# don't trip "Duplicated timeseries" — the second import wins the same
# Counter object back from the registry.
# ---------------------------------------------------------------------------


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


def _gauge(name: str, doc: str) -> Gauge:
    existing = getattr(REGISTRY, "_names_to_collectors", {}).get(name)
    if existing is not None:
        return existing
    return Gauge(name, doc)


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
escrow_locked_total = _counter(
    "agentnet_escrow_locked_total",
    "Escrow locks issued (task created with funds reserved).",
    ["currency"],
)
escrow_released_total = _counter(
    "agentnet_escrow_released_total",
    "Escrow releases on successful task confirmation.",
    ["currency"],
)
escrow_refunded_total = _counter(
    "agentnet_escrow_refunded_total",
    "Escrow refunds on task fail / timeout.",
    ["currency", "reason"],
)
span_persist_failures_total = _counter(
    "agentnet_span_persist_failures_total",
    "Span DB inserts that raised — hint that traces are missing.",
    [],
)
active_websocket_connections = _gauge(
    "agentnet_active_websocket_connections",
    "Live WebSocket connections held by this process.",
)


def make_metrics_middleware(service_name: str):
    """Build a Starlette middleware that times every request and bumps counters."""

    class MetricsMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            # Skip /metrics itself to avoid recursion / cardinality blowup.
            if request.url.path == "/metrics":
                return await call_next(request)
            start = time.perf_counter()
            response = await call_next(request)
            duration = time.perf_counter() - start
            # Use the route template (e.g. /tasks/{id}) where possible to
            # keep cardinality low; fall back to the literal path.
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
        except Exception as e:  # broad on purpose — any DB error = not ready
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
    """Wire health/metrics endpoints + request-counting middleware into ``app``."""
    app.add_middleware(make_metrics_middleware(service_name))
    app.include_router(make_health_router(service_name))
