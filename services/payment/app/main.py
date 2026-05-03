import logging
import os
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .api import router as api_router
from .api.rate_limiter import RateLimitMiddleware
from .database import Base, engine
from .health import install_health_and_metrics
from .logging_config import install_request_id_middleware, setup_logging
from .security import setup_cors, setup_security_headers
from .tracing import configure_tracing

setup_logging("payment")
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="AgentNet Payment Service",
    description="Payment service for AgentNet Protocol v2.0",
    version="2.0.0",
)

# Bind a request_id to every log line emitted while the request is in flight.
install_request_id_middleware(app)

# Mount rate limiting middleware (60 req/min/IP, Redis-backed for replicas).
from .config import REDIS_URL as _REDIS_URL  # noqa: E402
app.add_middleware(
    RateLimitMiddleware,
    max_requests=60,
    window_seconds=60,
    redis_url=_REDIS_URL,
)

# Configure security (CORS and headers)
setup_cors(app)
setup_security_headers(app)

# /healthz, /readyz, /metrics + per-request metrics middleware
install_health_and_metrics(app, service_name="payment")

# Configure tracing
tracer_provider = configure_tracing(app, engine)

# Include API router
app.include_router(api_router)


# Startup event
@app.on_event("startup")
async def startup_event():
    logger.info("Payment service started")


# Shutdown event
@app.on_event("shutdown")
async def shutdown_event():
    # Clean up resources
    if tracer_provider:
        await tracer_provider.shutdown()
    logger.info("Payment service shutdown")


# Legacy /health alias — keeps existing dashboards working.
@app.get("/health")
async def health_check_legacy():
    return {"status": "ok"}


# Root endpoint
@app.get("/")
async def root():
    return {
        "message": "AgentNet Payment Service",
        "version": "2.0.0",
        "docs": "/docs",
    }


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
        "app.main:app",
        host="0.0.0.0",
        port=8001,
        reload=True,
    )