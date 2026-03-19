"""
AgentNet Simulation Service — MiroFish Swarm Intelligence Integration.

Provides multi-agent social simulation capabilities powered by OASIS engine.
Integrates with AgentNet's agent registry, social graph, and escrow system.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import router as api_router
from .config import SimulationConfig
from .database import engine
from .security import setup_cors, setup_security_headers
from .tracing import configure_tracing

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="AgentNet Simulation Service",
    description="Swarm intelligence simulation powered by MiroFish/OASIS engine",
    version="1.0.0",
)

# Configure security
setup_cors(app)
setup_security_headers(app)

# Configure tracing
tracer_provider = configure_tracing(app, engine)

# Include API router
app.include_router(api_router)


@app.on_event("startup")
async def startup_event():
    errors = SimulationConfig.validate()
    if errors:
        for err in errors:
            logger.warning(f"Config warning: {err}")
    logger.info("Simulation service started")


@app.on_event("shutdown")
async def shutdown_event():
    if tracer_provider:
        await tracer_provider.shutdown()
    logger.info("Simulation service shutdown")


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
