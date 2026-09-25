"""Stream wake-ups and stream bounds (ADR-0009 D5, D8).

Redis pub/sub (``a2a:task:<id>``) only WAKES waiting streams; a stream
always re-reads the PostgreSQL event log after its last ``seq``. If Redis is
down, streams fall back to polling once a second — slower, never lossy.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from contextlib import contextmanager
from typing import Dict, Iterator, Optional, Set

logger = logging.getLogger(__name__)

CHANNEL_PREFIX = "a2a:task:"
POLL_SECONDS = 1.0


class Fanout:
    def __init__(self) -> None:
        self._waiters: Dict[str, Set[asyncio.Event]] = defaultdict(set)
        self._redis = None
        self._listener: Optional[asyncio.Task] = None

    async def start(self, redis_url: Optional[str]) -> None:
        if not redis_url or self._listener is not None:
            return
        try:
            import redis.asyncio as redis

            self._redis = redis.from_url(redis_url, encoding="utf-8", decode_responses=True)
            pubsub = self._redis.pubsub()
            await pubsub.psubscribe(CHANNEL_PREFIX + "*")
            self._listener = asyncio.create_task(self._listen(pubsub))
        except Exception as exc:  # noqa: BLE001 - degrade to polling
            logger.warning("a2a fanout: Redis unavailable, streams poll the event log (%s)", type(exc).__name__)
            self._redis = None

    async def stop(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
            self._listener = None
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._redis = None

    async def _listen(self, pubsub) -> None:
        try:
            async for item in pubsub.listen():
                if item.get("type") == "pmessage":
                    channel = str(item.get("channel", ""))
                    self._wake_local(channel[len(CHANNEL_PREFIX):])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("a2a fanout listener stopped (%s); streams keep polling", type(exc).__name__)

    def _wake_local(self, task_id: str) -> None:
        for event in list(self._waiters.get(task_id, ())):
            event.set()

    async def publish(self, task_id: uuid.UUID) -> None:
        key = str(task_id)
        self._wake_local(key)
        if self._redis is not None:
            try:
                await self._redis.publish(CHANNEL_PREFIX + key, "1")
            except Exception:  # noqa: BLE001 - the event log is authoritative
                pass

    @contextmanager
    def waiter(self, task_id: uuid.UUID) -> Iterator[asyncio.Event]:
        key = str(task_id)
        event = asyncio.Event()
        self._waiters[key].add(event)
        try:
            yield event
        finally:
            self._waiters[key].discard(event)
            if not self._waiters[key]:
                self._waiters.pop(key, None)

    @staticmethod
    async def wait(event: asyncio.Event, timeout: float = POLL_SECONDS) -> None:
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        event.clear()


class StreamLimiter:
    """Per-replica bounds on concurrent SSE streams (per principal, per task)."""

    def __init__(self) -> None:
        self._by_principal: Dict[str, int] = defaultdict(int)
        self._by_task: Dict[str, int] = defaultdict(int)

    def acquire(self, principal_key: str, task_id: Optional[uuid.UUID], *, per_principal: int, per_task: int) -> bool:
        tkey = str(task_id) if task_id else None
        if self._by_principal[principal_key] >= per_principal:
            return False
        if tkey is not None and self._by_task[tkey] >= per_task:
            return False
        self._by_principal[principal_key] += 1
        if tkey is not None:
            self._by_task[tkey] += 1
        return True

    def release(self, principal_key: str, task_id: Optional[uuid.UUID]) -> None:
        self._by_principal[principal_key] = max(0, self._by_principal[principal_key] - 1)
        if not self._by_principal[principal_key]:
            self._by_principal.pop(principal_key, None)
        if task_id is not None:
            tkey = str(task_id)
            self._by_task[tkey] = max(0, self._by_task[tkey] - 1)
            if not self._by_task[tkey]:
                self._by_task.pop(tkey, None)

    def active(self) -> int:
        return sum(self._by_principal.values())
