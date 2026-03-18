import asyncio
import logging
import os
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .a2a import build_registry_card
from .api import router as api_router
from .database import Base, engine
from .security import setup_cors, setup_security_headers
from .tracing import configure_tracing
from .websocket_manager import manager

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
setup_security_headers(app)

# Configure tracing
tracer_provider = configure_tracing(app, engine)

# Include API router
app.include_router(api_router)


# Startup event
@app.on_event("startup")
async def startup_event():
    # Initialize Redis connection for WebSocket manager
    await manager.init_redis()
    logger.info("Registry service started")


# Shutdown event
@app.on_event("shutdown")
async def shutdown_event():
    # Clean up resources
    if tracer_provider:
        await tracer_provider.shutdown()
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
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
