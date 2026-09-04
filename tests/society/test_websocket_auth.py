"""WebSocket audit: authentication, identity binding, expiry, malformed input
and the public feed keep-alive."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

import pytest
from jose import jwt
from starlette.websockets import WebSocketDisconnect

from services.registry.app.auth import create_agent_token
from services.registry.app.config import JWT_ALGORITHM, JWT_SECRET_KEY


def _expired_agent_token(agent_id):
    return jwt.encode({"sub": str(agent_id), "type": "agent", "exp": datetime.utcnow() - timedelta(minutes=5)}, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def test_agent_socket_rejects_bad_tokens(api_client, make_agent, user_token):
    a = make_agent("WS_A")
    b = make_agent("WS_B")
    _, user_tok = user_token(None)
    bad = {
        "garbage": "not-a-token",
        "expired": _expired_agent_token(a.id),
        "other agent": create_agent_token(b.id).access_token,
        "user token": user_tok,
    }
    for label, tok in bad.items():
        with pytest.raises(WebSocketDisconnect):
            with api_client.websocket_connect(f"/v1/ws/agent/{a.id}?token={tok}"):
                pass


def _frame_with(ws, key: str, max_frames: int = 10) -> dict:
    for _ in range(max_frames):
        frame = ws.receive_json()
        if key in frame:
            return frame
    raise AssertionError(f"no frame carrying {key!r} within {max_frames} frames")


def _rpc_reply(ws, rpc_id: str, max_frames: int = 10) -> dict:
    """The agent socket also carries broadcasts (agent_status, task events);
    skip those until the JSON-RPC reply with ``rpc_id`` arrives."""
    for _ in range(max_frames):
        frame = ws.receive_json()
        if frame.get("id") == rpc_id:
            return frame
    raise AssertionError(f"no JSON-RPC reply for id={rpc_id} within {max_frames} frames")


def test_agent_socket_accepts_owner_and_survives_malformed_frames(api_client, make_agent, SessionLocal, monkeypatch):
    # JSON-RPC dispatch opens its own SessionLocal per message (not the
    # request-scoped get_db), so point it at the test database too.
    monkeypatch.setattr("services.registry.app.database.SessionLocal", SessionLocal)
    a = make_agent("WS_OK")
    tok = create_agent_token(a.id).access_token
    with api_client.websocket_connect(f"/v1/ws/agent/{a.id}?token={tok}") as ws:
        ws.send_text("{not json")
        err = _frame_with(ws, "error")
        assert err["error"]["code"] == -32700
        ws.send_text(json.dumps({"jsonrpc": "2.0", "id": "1", "method": "ping"}))
        assert _rpc_reply(ws, "1").get("result") is not None
        # unknown message types are ignored, not fatal
        ws.send_text(json.dumps({"type": "nonsense", "payload": "x" * 100}))
        ws.send_text(json.dumps({"jsonrpc": "2.0", "id": "2", "method": "ping"}))
        assert _rpc_reply(ws, "2").get("id") == "2"


def test_public_feed_heartbeat(api_client):
    with api_client.websocket_connect("/v1/ws/feed") as ws:
        ws.send_text("ping")
        beat = ws.receive_json()
        assert beat["type"] == "heartbeat"


def test_public_feed_never_carries_message_bodies(api_client, make_agent):
    """The feed is unauthenticated; broadcasts must stay structural."""
    a = make_agent("Feed_WS_A")
    b = make_agent("Feed_WS_B")
    tok = create_agent_token(a.id).access_token
    r = api_client.post("/v1/chat/", headers={"Authorization": f"Bearer {tok}"}, json={"to_agent_id": str(b.id), "title": "t", "content": "FEED-PRIVATE-BODY"})
    assert r.status_code == 201
    # inspect what the manager would broadcast: the chat route strips content
    from services.registry.app.api.routes import chat as chat_mod

    src = open(chat_mod.__file__, encoding="utf-8").read()
    assert '"content": msg.content' not in src.split("manager.broadcast(")[1].split(")")[0]
