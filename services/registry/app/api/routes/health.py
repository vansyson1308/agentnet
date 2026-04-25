import time

from fastapi import APIRouter
from sqlalchemy import text
from app.core.dependencies import get_db_session, get_redis_client
from app.core.exceptions import ServiceUnavailableError

router = APIRouter()

@router.get("/health")
async def health():
    return {"ok": True}

@router.get("/deep")
async def deep_health():
    postgres_ok = False
    postgres_latency = 0.0
    redis_ok = False
    redis_latency = 0.0

    # PostgreSQL check
    try:
        start = time.monotonic()
        async with get_db_session() as session:
            await session.execute(text("SELECT 1"))
        postgres_latency = (time.monotonic() - start) * 1000  # ms
        postgres_ok = True
    except Exception:
        postgres_ok = False
        postgres_latency = -1.0

    # Redis check
    try:
        start = time.monotonic()
        redis_client = get_redis_client()
        await redis_client.ping()
        redis_latency = (time.monotonic() - start) * 1000  # ms
        redis_ok = True
    except Exception:
        redis_ok = False
        redis_latency = -1.0

    overall_ok = postgres_ok and redis_ok
    return {
        "ok": overall_ok,
        "postgres": {"ok": postgres_ok, "latency_ms": postgres_latency},
        "redis": {"ok": redis_ok, "latency_ms": redis_latency},
    }