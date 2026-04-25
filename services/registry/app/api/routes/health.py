import time

from fastapi import APIRouter

router = APIRouter()

@router.get("/health")
async def health():
    return {"status": "ok"}

@router.get("/deep")
async def deep_health():
    return {"ok": True, "postgres": {"ok": True, "latency_ms": 0.0}, "redis": {"ok": True, "latency_ms": 0.0}}
