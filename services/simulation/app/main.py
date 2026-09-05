"""
AgentNet Simulation Service — MiroFish Swarm Intelligence Integration.

Provides multi-agent social simulation capabilities powered by OASIS engine.
Integrates with AgentNet's agent registry, social graph, and escrow system.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import router as api_router
from .config import SimulationConfig
from .database import engine
from .health import install_health_and_metrics
from .logging_config import install_request_id_middleware, setup_logging
from .security import setup_cors, setup_security_headers
from .tracing import configure_tracing

setup_logging("simulation")
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown in one place (FastAPI lifespan; the startup/shutdown
    event decorators are deprecated). Config validation only warns — the
    service runs without an LLM key. ``TracerProvider.shutdown()`` is
    synchronous, so it is called, not awaited."""
    errors = SimulationConfig.validate()
    if errors:
        for err in errors:
            logger.warning(f"Config warning: {err}")
    logger.info("Simulation service started")
    try:
        yield
    finally:
        if tracer_provider:
            tracer_provider.shutdown()
        logger.info("Simulation service shutdown")


# Create FastAPI app
app = FastAPI(
    title="AgentNet Simulation Service",
    description="Swarm intelligence simulation powered by MiroFish/OASIS engine",
    version="1.0.0",
    lifespan=lifespan,
)

# Bind a request_id to every log line.
install_request_id_middleware(app)

# Configure security
setup_cors(app)
setup_security_headers(app)

# /healthz, /readyz, /metrics + per-request metrics middleware
install_health_and_metrics(app, service_name="simulation")

# Configure tracing
tracer_provider = configure_tracing(app, engine)

# Include API router
app.include_router(api_router)


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "llm_configured": SimulationConfig.is_llm_configured(),
        "zep_configured": SimulationConfig.is_zep_configured(),
    }


@app.get("/")
async def root():
    return {
        "message": "AgentNet Simulation Service",
        "version": "1.0.0",
        "docs": "/docs",
        "powered_by": "MiroFish/OASIS Swarm Intelligence Engine",
    }


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
        port=8002,
        reload=True,
    )
