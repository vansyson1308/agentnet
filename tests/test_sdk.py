"""
Light-weight tests for the SDK — no live services required.

We use httpx's MockTransport so the client thinks it's talking to the
registry / payment but every request resolves against a deterministic
handler. That lets us assert:

- Idempotency-Key header is generated automatically on create_task.
- Approval workflow methods hit the right paths.
- WebSocket URL converter handles http/https → ws/wss.
- The package imports cleanly with and without ``websockets``.
"""

from __future__ import annotations

import json
import pathlib
import sys

import httpx
import pytest

# Make the SDK importable without `pip install -e`. The package lives at
# sdk/python/agentnet so we just prepend sdk/python to sys.path.
_SDK_PATH = pathlib.Path(__file__).resolve().parent.parent / "sdk" / "python"
if str(_SDK_PATH) not in sys.path:
    sys.path.insert(0, str(_SDK_PATH))


def _make_client(handler):
    """Build an AgentNetClient whose internal httpx talks to ``handler``."""
    from agentnet.client import AgentNetClient

    transport = httpx.MockTransport(handler)
    client = AgentNetClient(
        registry_url="http://reg.test", payment_url="http://pay.test", timeout=2.0
    )
    client._client = httpx.Client(transport=transport, timeout=2.0)
    client._user_token = "fake-token"  # bypass auth checks in tests
    return client


def test_create_task_auto_generates_idempotency_key():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/tasks/" and request.method == "POST":
            captured["headers"] = dict(request.headers)
            return httpx.Response(
                201,
                json={
                    "task_session_id": "00000000-0000-0000-0000-000000000001",
                    "trace_id": "00000000-0000-0000-0000-000000000002",
                    "span_id": "00000000-0000-0000-0000-000000000003",
                },
            )
        return httpx.Response(404)

    client = _make_client(handler)
    task = client.create_task(
        caller_agent_id="00000000-0000-0000-0000-000000000010",
        callee_agent_id="00000000-0000-0000-0000-000000000020",
        capability="echo",
        input_data={"hi": "there"},
        max_budget=10,
    )
    assert task.id == "00000000-0000-0000-0000-000000000001"
    assert "idempotency-key" in {k.lower() for k in captured["headers"]}
    # 36-char uuid string.
    idem = next(v for k, v in captured["headers"].items() if k.lower() == "idempotency-key")
    assert len(idem) == 36


def test_create_task_passes_caller_supplied_idempotency_key():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            201,
            json={
                "task_session_id": "00000000-0000-0000-0000-000000000001",
                "trace_id": "00000000-0000-0000-0000-000000000002",
                "span_id": "00000000-0000-0000-0000-000000000003",
            },
        )

    client = _make_client(handler)
    client.create_task(
        caller_agent_id="00000000-0000-0000-0000-000000000010",
        callee_agent_id="00000000-0000-0000-0000-000000000020",
        capability="echo",
        input_data={"k": "v"},
        max_budget=1,
        idempotency_key="my-key-123",
    )
    sent = next(v for k, v in captured["headers"].items() if k.lower() == "idempotency-key")
    assert sent == "my-key-123"


def test_approval_workflow_routes():
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/v1/approval_requests/":
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == "/v1/approval_requests/":
            return httpx.Response(201, json={"id": "approval-1", "status": "pending"})
        if "approve" in request.url.path or "deny" in request.url.path:
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    client = _make_client(handler)
    assert client.list_approvals() == []
    created = client.create_approval(
        agent_id="00000000-0000-0000-0000-000000000010", amount=100, description="x"
    )
    assert created["id"] == "approval-1"
    client.approve("approval-1")
    client.deny("approval-1")
    paths = [p for _, p in calls]
    assert "/v1/approval_requests/" in paths
    assert "/v1/approval_requests/approval-1/approve" in paths
    assert "/v1/approval_requests/approval-1/deny" in paths


def test_health_and_ready_methods():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "service": "registry"})
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        return httpx.Response(404)

    client = _make_client(handler)
    assert client.health()["service"] == "registry"
    assert client.ready() is True


def test_ws_url_converter():
    pytest.importorskip("websockets")
    from agentnet.ws import _registry_to_ws

    assert _registry_to_ws("http://localhost:8000") == "ws://localhost:8000"
    assert _registry_to_ws("https://api.example.com") == "wss://api.example.com"
    assert _registry_to_ws("ws://already.ws") == "ws://already.ws"


def test_package_imports_without_websockets():
    """Importing `agentnet` must not fail just because `websockets` is absent.

    We simulate the missing-dep case by force-removing the ws module from
    sys.modules and re-importing the package — the optional re-export
    falls through to ``None`` and the package still loads."""
    import importlib
    import sys

    sys.modules.pop("agentnet", None)
    sys.modules.pop("agentnet.ws", None)
    pkg = importlib.import_module("agentnet")
    assert pkg.AgentNetClient is not None
    # AgentWebSocketClient may be None if `websockets` isn't installed.


# ─── WebSocket client: one adapter, two supported websockets APIs ──────────────
#
# agentnet/ws.py supports websockets 12 (classic asyncio client) and >= 13 (the
# new asyncio client, default since 14.0, where the classic one is deprecated).
# These tests run a real local WebSocket server built from whichever
# implementation is installed and exercise connect / send / recv / close /
# reconnect / auth headers end to end. scripts/ci/check_sdk_envs.sh runs this
# file at websockets 12.0 AND the current release.

import asyncio
import ast
import contextlib


@contextlib.asynccontextmanager
async def _echo_server(seen):
    websockets = pytest.importorskip("websockets")

    async def handler(conn):
        request = getattr(conn, "request", None)  # new API: conn.request; classic: request_headers/path
        headers = request.headers if request is not None else conn.request_headers
        seen["connections"] = seen.get("connections", 0) + 1
        seen["authorization"] = headers.get("Authorization")
        seen["path"] = request.path if request is not None else conn.path
        try:
            async for message in conn:
                await conn.send(message)  # echo
        except Exception:  # noqa: BLE001 - server side of a closed socket
            return

    try:
        from websockets.asyncio.server import serve  # websockets >= 13
    except ImportError:
        serve = websockets.serve  # websockets 12: classic server
    async with serve(handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield f"http://127.0.0.1:{port}"


async def _first_message(client):
    stream = client.recv()
    try:
        return await asyncio.wait_for(stream.__anext__(), 5)
    finally:
        await stream.aclose()


@pytest.mark.timeout(60)
async def test_ws_connect_send_recv_close():
    from agentnet import ws as wsmod

    seen = {}
    async with _echo_server(seen) as url:
        async with wsmod.AgentWebSocketClient(registry_url=url, agent_id="agent-1", token="t0k") as client:
            assert client.connected
            assert wsmod.CLIENT_API in ("asyncio", "classic")
            await client.task_start("task-1")
            msg = await _first_message(client)
            assert msg["jsonrpc"] == "2.0" and msg["method"] == "task_start"
            assert msg["params"] == {"task_id": "task-1"}
            await client.execute(callee_agent_id="callee", capability="cap", input_data={"q": 1}, max_budget=5)
            msg = await _first_message(client)
            assert msg["method"] == "execute" and msg["to"] == "callee" and msg["idempotency_key"]
        assert not client.connected
        assert seen["connections"] == 1
        assert seen["path"].endswith("/v1/ws/agent/agent-1?token=t0k")
    with pytest.raises(RuntimeError):
        await client.task_start("after-close")


@pytest.mark.timeout(60)
async def test_ws_reconnect_is_idempotent_and_headers_reach_the_server():
    from agentnet import ws as wsmod

    seen = {}
    async with _echo_server(seen) as url:
        client = wsmod.AgentWebSocketClient(
            registry_url=url, agent_id="agent-2", token="t", headers={"Authorization": "Bearer abc"}
        )
        await client.connect()
        await client.connect()  # a live connection is kept
        assert seen["connections"] == 1
        assert seen["authorization"] == "Bearer abc"
        await client.reconnect()
        assert seen["connections"] == 2
        await client.task_confirm("task-9", {"ok": True})
        msg = await _first_message(client)
        assert msg["method"] == "task_confirm" and msg["params"]["output"] == {"ok": True}
        await client.close()
        assert not client.connected
        await client.close()  # idempotent


@pytest.mark.timeout(60)
async def test_ws_recv_ends_cleanly_when_the_server_goes_away():
    from agentnet.ws import connect_agent

    seen = {}
    server = _echo_server(seen)
    url = await server.__aenter__()
    client_cm = connect_agent(registry_url=url, agent_id="agent-3", token="t")
    client = await client_cm.__aenter__()
    try:
        await client.task_fail("task-3", "boom")
        assert (await _first_message(client))["params"]["error_message"] == "boom"
        await server.__aexit__(None, None, None)  # server closes every connection
        remaining = [m async for m in client.recv()]  # ConnectionClosed is swallowed → generator ends
        assert remaining == []
    finally:
        await client_cm.__aexit__(None, None, None)
    assert not client.connected


def test_sdk_and_examples_never_import_deprecated_websockets_namespaces():
    """websockets.legacy and websockets.client.WebSocketClientProtocol warn
    on >= 14; the adapter in agentnet/ws.py is the only place allowed to know
    about client APIs and it only touches websockets.asyncio.client."""
    repo = pathlib.Path(__file__).resolve().parent.parent
    files = [repo / "sdk/python/agentnet/ws.py", repo / "examples/agent_sdk.py", repo / "examples/reference_agent.py"]
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("websockets.legacy", "websockets.client")), f"{path}: {node.module}"
            if isinstance(node, ast.Attribute) and node.attr == "WebSocketClientProtocol":
                raise AssertionError(f"{path}: deprecated WebSocketClientProtocol attribute access")
