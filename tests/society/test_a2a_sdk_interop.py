"""Interoperability with the OFFICIAL a2a-sdk client (pinned 1.1.5).

The official client resolves AgentNet's cards, picks an interface, applies
the tenant, and speaks both bindings to the real registry app (in process,
over ASGI). Nothing here is hand-rolled on the client side: if AgentNet's
wire format drifted from the SDK's, these tests fail.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid

import httpx
import pytest
from a2a.client import ClientCallContext, ClientConfig, ClientFactory
from a2a.client.service_parameters import ServiceParametersFactory, with_a2a_extensions
from a2a.types import a2a_pb2 as pb
from a2a.utils.errors import ExtensionSupportRequiredError
from google.protobuf.struct_pb2 import Struct

from services.registry.app.auth import create_agent_token

BASE = "https://api.agentnet.test"
ECON = "https://agentnet.io.vn/a2a/extensions/economics/v1"
CAPS = [
    {"name": "echo", "version": "1.0", "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "price": 0},
    {"name": "summarize", "version": "1.0", "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "price": 7},
]


@pytest.fixture
def env(monkeypatch, SessionLocal, db):
    from services.registry.app.a2a import handler as a2a_handler
    from services.registry.app.a2a import routes as a2a_routes
    from services.registry.app.main import app

    monkeypatch.setenv("A2A_SERVER_ENABLED", "true")
    monkeypatch.setenv("A2A_PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("A2A_BLOCKING_WAIT_SECONDS", "0")
    monkeypatch.setattr(a2a_routes.runtime, "session_factory", SessionLocal)

    async def no_dispatch(**kw):
        return None

    monkeypatch.setattr(a2a_handler, "dispatch_execute", no_dispatch)
    return app


def _client(app, token, bindings):
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    return http, ClientFactory(ClientConfig(httpx_client=http, streaming=False, supported_protocol_bindings=bindings))


def _message(text=None, data=None, metadata=None):
    parts = []
    if text is not None:
        parts.append(pb.Part(text=text))
    if data is not None:
        v = Struct()
        v.update(data)
        parts.append(pb.Part(data=__import__("google.protobuf.struct_pb2", fromlist=["Value"]).Value(struct_value=v)))
    m = pb.Message(message_id=uuid.uuid4().hex, role=pb.ROLE_USER, parts=parts)
    if metadata:
        m.metadata.update(metadata)
    return m


async def _first(client, request, context=None):
    async for event in client.send_message(request, context=context):
        return event
    raise AssertionError("no event")


@pytest.mark.parametrize("binding", ["JSONRPC", "HTTP+JSON"])
def test_official_client_network_card_and_marketplace_search(env, make_agent, binding):
    caller = make_agent("Interop_Caller", balance_credits=50)
    make_agent("Interop_Target", capabilities=CAPS)
    token = create_agent_token(caller.id).access_token

    async def run():
        http, factory = _client(env, token, [binding])
        async with http:
            client = await factory.create_from_url(BASE)
            assert client._card.name == "AgentNet"  # noqa: SLF001 - the resolved card
            event = await _first(client, pb.SendMessageRequest(message=_message(text="Interop_Target")))
            assert event.HasField("message")
            data = next(p.data for p in event.message.parts if p.HasField("data"))
            assert data.struct_value["count"] == 1

    asyncio.run(run())


@pytest.mark.parametrize("binding", ["JSONRPC", "HTTP+JSON"])
def test_official_client_drives_a_tenant_task_through_the_agent_card(env, make_agent, SessionLocal, binding):
    from services.registry.app.a2a.orm import A2ATask
    from services.registry.app.task_service import confirm_task_completion, start_task

    caller = make_agent("Interop_Caller2", balance_credits=50)
    target = make_agent("Interop_Target2", capabilities=CAPS)
    token = create_agent_token(caller.id).access_token

    async def run():
        http, factory = _client(env, token, [binding])
        async with http:
            client = await factory.create_from_url(BASE, relative_card_path=f"/v1/agents/{target.id}/a2a-card")
            # core client, free skill: works without any extension
            event = await _first(client, pb.SendMessageRequest(message=_message(data={"q": 1}, metadata={"skillId": "echo"}), configuration=pb.SendMessageConfiguration(return_immediately=True)))
            task = event.task
            assert task.status.state == pb.TASK_STATE_SUBMITTED

            s = SessionLocal()
            try:
                sid = s.query(A2ATask).filter(A2ATask.id == uuid.UUID(task.id)).one().task_session_id
                start_task(db=s, task_id=sid, callee_agent=s.merge(target))
                confirm_task_completion(db=s, callee_agent=s.merge(target), task_id=sid, output={"text": "hi"})
            finally:
                s.close()

            got = await client.get_task(pb.GetTaskRequest(id=task.id))
            assert got.status.state == pb.TASK_STATE_COMPLETED
            assert got.artifacts[0].parts[0].text == "hi"
            listed = await client.list_tasks(pb.ListTasksRequest(page_size=10))
            assert [t.id for t in listed.tasks] == [task.id] and listed.next_page_token == ""

            # paid skill: the core client gets the required-extension error ...
            with pytest.raises(ExtensionSupportRequiredError):
                await _first(client, pb.SendMessageRequest(message=_message(text="x", metadata={"skillId": "summarize"})))
            # ... and the extension-aware client is charged exactly the price
            ctx = ClientCallContext(service_parameters=ServiceParametersFactory.create([with_a2a_extensions([ECON])]))
            paid = await _first(
                client,
                pb.SendMessageRequest(
                    message=_message(text="x", metadata={"skillId": "summarize", ECON: {"maxBudget": 7, "currency": "credits"}}),
                    configuration=pb.SendMessageConfiguration(return_immediately=True),
                ),
                context=ctx,
            )
            assert paid.task.status.state == pb.TASK_STATE_SUBMITTED
            canceled = await client.cancel_task(pb.CancelTaskRequest(id=paid.task.id))
            assert canceled.status.state == pb.TASK_STATE_CANCELED

    asyncio.run(run())
    from services.registry.app.models import Wallet

    s = SessionLocal()
    try:
        w = s.query(Wallet).filter(Wallet.owner_id == caller.id).one()
        assert w.reserved_credits == 0 and w.balance_credits == 50
    finally:
        s.close()


def test_official_streaming_client_receives_the_task_then_updates(env, make_agent, SessionLocal):
    from services.registry.app.a2a.orm import A2ATask
    from services.registry.app.task_service import confirm_task_completion, start_task

    caller = make_agent("Interop_Streamer", balance_credits=10)
    target = make_agent("Interop_Worker", capabilities=CAPS)
    token = create_agent_token(caller.id).access_token

    def callee():
        s = SessionLocal()
        try:
            for _ in range(200):
                row = s.query(A2ATask).filter(A2ATask.caller_agent_id == caller.id).first()
                if row is not None and row.task_session_id:
                    break
                s.rollback()
                time.sleep(0.05)
            start_task(db=s, task_id=row.task_session_id, callee_agent=s.merge(target))
            confirm_task_completion(db=s, callee_agent=s.merge(target), task_id=row.task_session_id, output={"text": "streamed"})
        finally:
            s.close()

    async def run():
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=env), base_url=BASE, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        async with http:
            factory = ClientFactory(ClientConfig(httpx_client=http, streaming=True, supported_protocol_bindings=["JSONRPC"]))
            client = await factory.create_from_url(BASE, relative_card_path=f"/v1/agents/{target.id}/a2a-card")
            kinds, states = [], []
            async for event in client.send_message(pb.SendMessageRequest(message=_message(data={}, metadata={"skillId": "echo"}))):
                kind = event.WhichOneof("payload")
                kinds.append(kind)
                if kind == "status_update":
                    states.append(event.status_update.status.state)
            return kinds, states

    worker = threading.Thread(target=callee)
    worker.start()
    kinds, states = asyncio.run(run())
    worker.join(30)
    assert kinds[0] == "task" and "artifact_update" in kinds
    assert states[-1] == pb.TASK_STATE_COMPLETED
