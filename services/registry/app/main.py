import asyncio
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .a2a import build_registry_card
from .api import router as api_router
from .api.rate_limiter import RateLimitMiddleware
from .config import MANAGED_EXECUTION_ENABLED, REDIS_URL
from .database import SessionLocal, engine
from .health import install_health_and_metrics
from .logging_config import install_request_id_middleware, setup_logging
from .run_service import expire_stale_leases
from .security import setup_cors, setup_security_headers
from .tracing import configure_tracing
from .websocket_manager import manager

# Configure structured logging — JSON in prod, console in dev. Must run
# before any module-level logger is instantiated below so the formatter
# applies uniformly.
setup_logging("registry")
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="AgentNet Registry Service",
    description="Registry service for AgentNet Protocol v2.0",
    version="2.0.0",
)

# Bind a request_id (uuid4 or honour inbound X-Request-ID) to every
# log line emitted while the request is in flight.
install_request_id_middleware(app)

# Configure security (CORS and headers). setup_cors() already attaches the
# Cloudflare-tunnel regex when ENVIRONMENT=development; in prod it sticks
# strictly to CORS_ALLOWED_ORIGINS.
setup_cors(app)
# Mount rate limiter (Redis-backed when available, in-memory fallback).
app.add_middleware(
    RateLimitMiddleware,
    default_rate=int(os.getenv("RATE_LIMIT_USER_PER_MIN", "100")),
    default_burst=int(os.getenv("RATE_LIMIT_USER_BURST", "150")),
    agent_rate=int(os.getenv("RATE_LIMIT_AGENT_PER_MIN", "300")),
    agent_burst=int(os.getenv("RATE_LIMIT_AGENT_BURST", "450")),
    redis_url=os.getenv("REDIS_URL_RATE_LIMIT") or REDIS_URL,
)
setup_security_headers(app)

# Health, readiness, Prometheus metrics — mount BEFORE the API router
# so the /metrics middleware sees every request.
install_health_and_metrics(app, service_name="registry")

# Configure tracing
tracer_provider = configure_tracing(app, engine)

# Include API router
app.include_router(api_router)

# Background task reference for auto-scaler
_auto_scaler_task = None
_lease_reconciler_task = None


async def _lease_reconciler_loop():
    while True:
        db = SessionLocal()
        try:
            expired = await asyncio.to_thread(expire_stale_leases, db)
            if expired:
                logger.warning("Expired %d stale managed leases", expired)
        except Exception:
            db.rollback()
            logger.exception("Managed lease reconciler failed")
        finally:
            db.close()
        await asyncio.sleep(15)


# Startup event
@app.on_event("startup")
async def startup_event():
    global _auto_scaler_task, _lease_reconciler_task
    # Initialize Redis connection for WebSocket manager
    await manager.init_redis()
    # The registry auto-scaler is a legacy competing dispatcher. Managed
    # execution keeps allocation in runtime_allocator and leaves this off by
    # default; operators may briefly re-enable it only for legacy rollback.
    if os.getenv("LEGACY_AUTO_SCALER_ENABLED", "false").lower() == "true":
        from .auto_scaler import start_auto_scaler

        _auto_scaler_task = await start_auto_scaler()
    else:
        logger.info("Legacy auto-scaler disabled; AgentNet allocator owns dispatch")
    if MANAGED_EXECUTION_ENABLED:
        _lease_reconciler_task = asyncio.create_task(_lease_reconciler_loop())
    logger.info("Registry service started")


# Shutdown event
@app.on_event("shutdown")
async def shutdown_event():
    global _auto_scaler_task, _lease_reconciler_task
    # Clean up resources
    if tracer_provider:
        await tracer_provider.shutdown()
    # Stop auto-scaler gracefully
    if _auto_scaler_task is not None:
        from .auto_scaler import stop_auto_scaler

        await stop_auto_scaler()
        _auto_scaler_task = None
        logger.info("Auto-scaler stopped")
    if _lease_reconciler_task is not None:
        _lease_reconciler_task.cancel()
        try:
            await _lease_reconciler_task
        except asyncio.CancelledError:
            pass
        _lease_reconciler_task = None
    logger.info("Registry service shutdown")


# Legacy /health alias — keeps existing dashboards working. New
# /healthz, /readyz, /metrics are installed by install_health_and_metrics.
@app.get("/health")
async def health_check_legacy():
    return {"status": "ok"}


# Root endpoint
@app.get("/")
async def root():
    return {
        "message": "AgentNet Registry Service",
        "version": "2.0.0",
        "docs": "/docs",
        "a2a_card": "/.well-known/agent-card.json",
    }


# A2A Agent Card — discovery endpoint per RFC 8615
@app.get("/.well-known/agent-card.json")
async def get_registry_agent_card(request: Request):
    """
    Serve the A2A Agent Card for the AgentNet Registry.

    Behind a reverse proxy (Caddy), ``request.base_url`` resolves to
    the internal hostname (``http://127.0.0.1:8000``), which is useless
    to remote callers. Honour the X-Forwarded-* headers populated by
    the proxy so the advertised URL matches what callers actually used.
    Falls back to ``request.base_url`` when running standalone.
    """
    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if forwarded_proto and forwarded_host:
        base_url = f"{forwarded_proto}://{forwarded_host}"
    else:
        base_url = str(request.base_url).rstrip("/")
    card = build_registry_card(base_url=base_url.rstrip("/"))
    return card.model_dump(by_alias=True, exclude_none=True)


# Exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:main",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
