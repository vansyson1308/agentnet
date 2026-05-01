import asyncio
import logging
import os
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware import Middleware

from .a2a import build_registry_card
from .api import router as api_router
from .database import Base, engine
from .security import setup_cors, setup_security_headers
from .tracing import configure_tracing
from .websocket_manager import manager
from .api.rate_limiter import add_rate_limiter, RateLimitMiddleware
from .auto_scaler import start_auto_scaler, stop_auto_scaler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="AgentNet Registry Service",
    description="Registry service for AgentNet Protocol v2.0",
    version="2.0.0",
)

# Configure security (CORS and headers)
setup_cors(app)
# Add allow_origin_regex for Cloudflare tunnels
for i, mw in enumerate(app.user_middleware):
    if mw.cls == CORSMiddleware:
        options = dict(mw.options)
        options["allow_origin_regex"] = r"https://.*\.trycloudflare\.com$"
        app.user_middleware[i] = Middleware(CORSMiddleware, **options)
        break
# Mount rate limiter (env-configurable, defaults: 100 req/min users, 300 req/min agents)
import os as _os
app.add_middleware(
    RateLimitMiddleware,
    default_rate=int(_os.getenv("RATE_LIMIT_USER_PER_MIN", "100")),
    default_burst=int(_os.getenv("RATE_LIMIT_USER_BURST", "150")),
    agent_rate=int(_os.getenv("RATE_LIMIT_AGENT_PER_MIN", "300")),
    agent_burst=int(_os.getenv("RATE_LIMIT_AGENT_BURST", "450")),
    redis_url=_os.getenv("REDIS_URL", None),  # None = in-memory mode
)
setup_security_headers(app)

# Configure tracing
tracer_provider = configure_tracing(app, engine)

# Include API router
app.include_router(api_router)

# Background task reference for auto-scaler
_auto_scaler_task = None

# Startup event
@app.on_event("startup")
async def startup_event():
    global _auto_scaler_task
    # Initialize Redis connection for WebSocket manager
    await manager.init_redis()
    # Start auto-scaler background task
    _auto_scaler_task = await start_auto_scaler()
    logger.info("Registry service started")


# Shutdown event
@app.on_event("shutdown")
async def shutdown_event():
    global _auto_scaler_task
    # Clean up resources
    if tracer_provider:
        await tracer_provider.shutdown()
    # Stop auto-scaler gracefully
    if _auto_scaler_task is not None:
        await stop_auto_scaler()
        _auto_scaler_task = None
        logger.info("Auto-scaler stopped")
    logger.info("Registry service shutdown")


# Health check endpoint
@app.get("/health")
async def health_check():
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

    Any A2A-compatible system can GET this endpoint to discover
    what this registry offers and how to interact with it.
    """
    base_url = str(request.base_url).rstrip("/")
    card = build_registry_card(base_url=base_url)
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