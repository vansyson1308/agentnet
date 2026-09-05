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

Supported ``websockets`` releases: ``>=12,<18`` (``agentnet[ws]``). The
package rewrote its asyncio client in 13.0 and deprecated the original one
in 14.0 (``websockets.legacy`` / ``websockets.client.WebSocketClientProtocol``
now warn). :func:`_client_connect` is the ONE place that knows about the two
APIs — the rest of this module only relies on the behaviour they share:
``await connect(url)`` → connection, ``send``, ``async for`` iteration,
``close`` and ``websockets.exceptions.ConnectionClosed``.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover
    websockets = None

    class ConnectionClosed(Exception):  # type: ignore[no-redef]
        """Placeholder so the module imports without ``websockets``."""


#: The websockets client API this process ended up with: ``"asyncio"``
#: (websockets >= 13, ``websockets.asyncio.client``) or ``"classic"``
#: (websockets 12, ``websockets.connect`` — not deprecated at that version).
CLIENT_API: Optional[str] = None


def _client_connect(url: str, headers: Optional[Dict[str, str]] = None):
    """Return the connect awaitable for whichever client API is installed.

    * websockets >= 13: ``websockets.asyncio.client.connect`` — the current
      implementation (default since 14.0); extra request headers are passed
      as ``additional_headers``.
    * websockets 12: ``websockets.connect`` — the classic implementation,
      not deprecated at that version; the same headers are ``extra_headers``.

    Never imports ``websockets.legacy`` or ``websockets.client``, so no
    deprecated namespace is touched on any supported release.
    """
    global CLIENT_API
    if websockets is None:  # pragma: no cover - guarded by the constructor
        raise ImportError("websockets is not installed")
    try:
        from websockets.asyncio.client import connect  # websockets >= 13
    except ImportError:
        CLIENT_API = "classic"
        if headers:
            return websockets.connect(url, extra_headers=headers)
        return websockets.connect(url)
    CLIENT_API = "asyncio"
    if headers:
        return connect(url, additional_headers=headers)
    return connect(url)


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
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        if websockets is None:
            raise ImportError(
                "agentnet[ws] requires the optional `websockets` package: "
                "pip install websockets"
            )
        ws_base = _registry_to_ws(registry_url.rstrip("/"))
        self._url = f"{ws_base}/v1/ws/agent/{agent_id}?token={token}"
        self._headers = dict(headers) if headers else None
        self._conn: Optional[Any] = None

    @property
    def connected(self) -> bool:
        return self._conn is not None

    async def connect(self) -> "AgentWebSocketClient":
        """Open the connection (idempotent: a live connection is kept)."""
        if self._conn is None:
            self._conn = await _client_connect(self._url, self._headers)
        return self

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await conn.close()

    async def reconnect(self) -> "AgentWebSocketClient":
        """Drop the current connection (if any) and open a fresh one."""
        await self.close()
        return await self.connect()

    async def __aenter__(self) -> "AgentWebSocketClient":
        return await self.connect()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

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
        """Yield decoded JSON messages from the registry until the connection closes."""
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
        except ConnectionClosed:
            return


@asynccontextmanager
async def connect_agent(
    *, registry_url: str, agent_id: str, token: str, headers: Optional[Dict[str, str]] = None
) -> AsyncIterator[AgentWebSocketClient]:
    """Convenience wrapper so callers can ``async with connect_agent(...) as ws:``."""
    async with AgentWebSocketClient(
        registry_url=registry_url, agent_id=agent_id, token=token, headers=headers
    ) as ws:
        yield ws
