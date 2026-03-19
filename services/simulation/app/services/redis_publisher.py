"""
Redis publisher — broadcast simulation progress updates.

Publishes to 'simulation_updates' channel for real-time frontend consumption.
"""

import json
import logging
import os
import uuid
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

REDIS_URL = (
    f"redis://:{os.getenv('REDIS_PASSWORD', 'your_redis_password')}"
    f"@{os.getenv('REDIS_HOST', 'redis')}"
    f":{os.getenv('REDIS_PORT', '6379')}/0"
)

CHANNEL = "simulation_updates"

_redis_client: Optional[aioredis.Redis] = None


async def get_redis() -> Optional[aioredis.Redis]:
    """Get or create a Redis connection."""
    global _redis_client
    if _redis_client is None:
        try:
            _redis_client = await aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
            await _redis_client.ping()
            logger.info("Redis publisher connected")
        except Exception as e:
            logger.warning(f"Redis not available for publishing: {e}")
            _redis_client = None
    return _redis_client


async def publish_progress(
    sim_id: uuid.UUID,
    user_id: uuid.UUID,
    progress_pct: int,
    message: str,
    status: str = "running",
) -> None:
    """
    Publish simulation progress to Redis pub/sub.

    Message format:
    {
        "simulation_id": "...",
        "user_id": "...",
        "progress_pct": 42,
        "message": "Running social simulation...",
        "status": "running"
    }
    """
    client = await get_redis()
    if client is None:
        return

    try:
        payload = json.dumps(
            {
                "simulation_id": str(sim_id),
                "user_id": str(user_id),
                "progress_pct": progress_pct,
                "message": message,
                "status": status,
            }
        )
        await client.publish(CHANNEL, payload)
    except Exception as e:
        logger.warning(f"Failed to publish simulation progress: {e}")


async def publish_completed(sim_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Publish simulation completion event."""
    await publish_progress(
        sim_id=sim_id,
        user_id=user_id,
        progress_pct=100,
        message="Simulation completed!",
        status="completed",
    )


async def publish_failed(sim_id: uuid.UUID, user_id: uuid.UUID, error: str) -> None:
    """Publish simulation failure event."""
    await publish_progress(
        sim_id=sim_id,
        user_id=user_id,
        progress_pct=-1,
        message=f"Simulation failed: {error}",
        status="failed",
    )


async def close() -> None:
    """Close the Redis connection."""
    global _redis_client
    if _redis_client:
        await _redis_client.close()
        _redis_client = None
