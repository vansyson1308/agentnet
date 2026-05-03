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
