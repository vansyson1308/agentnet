"""
Minimal WebSocket client for AgentNet.

The registry routes ``/v1/ws/agent/{agent_id}?token=...`` are the
inbound channel for an agent: caller agents push ``method=execute``
JSON-RPC envelopes here, and a callee agent dispatches its own
``task_start`` / ``task_confirm`` / ``task_fail`` messages back.

Usage::

    from agentnet.ws import AgentWebSocketClient

    async def on_message(msg):
        # msg is a dict — handle method=execute, etc.
        ...

    async with AgentWebSocketClient(
        registry_url="http://localhost:8000",
        agent_id="...",
        token="...",
    ) as ws:
        await ws.task_start("...")
        await ws.task_confirm("...", output={"result": "ok"})
        async for msg in ws.recv():
            await on_message(msg)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional

try:
    import websockets
    from websockets.client import WebSocketClientProtocol
except ImportError:  # pragma: no cover
    websockets = None
    WebSocketClientProtocol = Any  # type: ignore


def _registry_to_ws(registry_url: str) -> str:
    if registry_url.startswith("https://"):
        return "wss://" + registry_url[len("https://") :]
    if registry_url.startswith("http://"):
        return "ws://" + registry_url[len("http://") :]
    return registry_url


class AgentWebSocketClient:
    """Async client for the agent WebSocket endpoint."""

    def __init__(
        self,
        *,
        registry_url: str,
        agent_id: str,
        token: str,
    ) -> None:
        if websockets is None:
            raise ImportError(
                "agentnet[ws] requires the optional `websockets` package: "
                "pip install websockets"
            )
        ws_base = _registry_to_ws(registry_url.rstrip("/"))
        self._url = f"{ws_base}/v1/ws/agent/{agent_id}?token={token}"
        self._conn: Optional[WebSocketClientProtocol] = None

    async def __aenter__(self) -> "AgentWebSocketClient":
        self._conn = await websockets.connect(self._url)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _send(self, message: Dict[str, Any]) -> None:
        if self._conn is None:
            raise RuntimeError("Not connected — use `async with AgentWebSocketClient(...)`")
        await self._conn.send(json.dumps(message))

    async def task_start(self, task_id: str) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "task_start",
                "params": {"task_id": task_id},
            }
        )

    async def task_confirm(self, task_id: str, output: Dict[str, Any]) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "task_confirm",
                "params": {"task_id": task_id, "output": output},
            }
        )

    async def task_fail(self, task_id: str, error_message: str) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "task_fail",
                "params": {"task_id": task_id, "error_message": error_message},
            }
        )

    async def execute(
        self,
        *,
        callee_agent_id: str,
        capability: str,
        input_data: Dict[str, Any],
        max_budget: int,
        currency: str = "credits",
        timeout_seconds: int = 300,
        idempotency_key: Optional[str] = None,
    ) -> None:
        """Initiate a task on a remote agent via WS (parity with REST POST /tasks/)."""
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "execute",
                "to": callee_agent_id,
                "idempotency_key": idempotency_key or str(uuid.uuid4()),
                "params": {
                    "capability": capability,
                    "input": input_data,
                    "payment": {"max_budget": max_budget, "currency": currency},
                    "timeout_seconds": timeout_seconds,
                    "callee_agent_id": callee_agent_id,
                },
            }
        )

    async def recv(self) -> AsyncIterator[Dict[str, Any]]:
        """Yield decoded JSON messages from the registry forever."""
        if self._conn is None:
            raise RuntimeError("Not connected")
        try:
            async for raw in self._conn:
                if isinstance(raw, bytes):
                    raw = raw.decode()
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    continue
        except websockets.ConnectionClosed:
            return


@asynccontextmanager
async def connect_agent(
    *, registry_url: str, agent_id: str, token: str
) -> AsyncIterator[AgentWebSocketClient]:
    """Convenience wrapper so callers can ``async with connect_agent(...) as ws:``."""
    async with AgentWebSocketClient(
        registry_url=registry_url, agent_id=agent_id, token=token
    ) as ws:
        yield ws
