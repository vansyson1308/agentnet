"""AgentNet as an A2A CLIENT of external agents (ADR-0009 D12).

Uses the official SDK client (``ClientFactory``) over ``netguard``'s pinned,
public-only transport. Rules:

* only ``verified`` catalog agents, through an active connection, while
  ``A2A_FEDERATION_ENABLED`` is on, within the connection's daily limit;
* every call is a durable ``a2a_outbound_calls`` row keyed by an idempotency
  key; a repeated key returns the recorded call instead of sending again;
* a send is NEVER retried (a remote task may have started); only reads
  (GetTask) retry once;
* the sealed credential is opened here, used as one header, and dropped;
* remote output is summarized, bounded and labelled untrusted.

No AgentNet escrow or wallet is involved: a remote agent has no AgentNet
economic identity.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from a2a.client import ClientConfig, ClientFactory
from a2a.types import a2a_pb2 as pb
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Value
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import config, metrics
from ..orm import A2AConnection, A2AOutboundCall, A2ARemoteAgent
from . import vault
from .netguard import OutboundRefused, safe_client

logger = logging.getLogger(__name__)
# The SDK logs whole remote cards at INFO; remote content never goes to our logs.
logging.getLogger("a2a.client.card_resolver").setLevel(logging.WARNING)

SEND_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_SUMMARY_TEXT = 1000
TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}


class OutboundRefusedByPolicy(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class OutboundRequest:
    connection_id: uuid.UUID
    skill_id: str
    input: Dict[str, Any]
    idempotency_key: str
    initiator_class: str  # "operator" | "society"
    initiator_id: Optional[uuid.UUID] = None
    correlation_id: Optional[uuid.UUID] = None
    causation_id: Optional[uuid.UUID] = None
    intent_id: Optional[uuid.UUID] = None
    depth: int = 1


def call_view(call: A2AOutboundCall) -> Dict[str, Any]:
    return {
        "id": str(call.id),
        "connectionId": str(call.connection_id) if call.connection_id else None,
        "remoteAgentId": str(call.remote_agent_id) if call.remote_agent_id else None,
        "initiator": call.initiator_class,
        "operation": call.operation,
        "skillId": call.skill_id,
        "status": call.status,
        "remoteTaskId": call.remote_task_id,
        "remoteState": call.remote_state,
        "errorClass": call.error_class,
        "result": call.result_summary,
        "depth": call.depth,
        "createdAt": call.created_at.isoformat() if call.created_at else None,
        "finishedAt": call.finished_at.isoformat() if call.finished_at else None,
        "untrusted": True,
    }


def _card_for(agent: A2ARemoteAgent) -> pb.AgentCard:
    """The client-side card: only the validated interfaces we stored."""
    interfaces = (agent.capabilities or {}).get("interfaces", [])
    return pb.AgentCard(
        name=agent.name or "remote",
        supported_interfaces=[
            pb.AgentInterface(url=i["url"], protocol_binding=i["binding"], protocol_version=i["version"], tenant=i.get("tenant", ""))
            for i in interfaces
        ],
        capabilities=pb.AgentCapabilities(streaming=False),
    )


def prepare(db: Session, req: OutboundRequest) -> A2AOutboundCall:
    """Policy checks + the durable call row (status ``pending``), added to the
    caller's transaction WITHOUT committing (the Society executor commits it
    with the intent). The request itself is kept so any process can deliver it."""
    existing = db.query(A2AOutboundCall).filter(A2AOutboundCall.idempotency_key == req.idempotency_key).first()
    if existing is not None:
        return existing
    if not config.federation_enabled():
        raise OutboundRefusedByPolicy("federation_disabled")
    if req.depth > config.max_federation_depth():
        raise OutboundRefusedByPolicy("federation_depth")
    conn = db.query(A2AConnection).filter(A2AConnection.id == req.connection_id).first()
    if conn is None or conn.revoked_at is not None:
        raise OutboundRefusedByPolicy("connection_unavailable")
    agent = db.query(A2ARemoteAgent).filter(A2ARemoteAgent.id == conn.remote_agent_id).first()
    if agent is None or agent.state != "verified":
        raise OutboundRefusedByPolicy("remote_agent_not_verified")
    if req.skill_id not in {s.get("id") for s in (agent.skills or [])}:
        raise OutboundRefusedByPolicy("unknown_remote_skill")
    since = datetime.now(timezone.utc) - timedelta(days=1)
    used = (
        db.query(func.count(A2AOutboundCall.id))
        .filter(A2AOutboundCall.connection_id == conn.id, A2AOutboundCall.created_at >= since, A2AOutboundCall.operation == "SendMessage")
        .scalar()
        or 0
    )
    if used >= int(conn.daily_call_limit or 0):
        raise OutboundRefusedByPolicy("daily_call_limit")
    call = A2AOutboundCall(
        id=uuid.uuid4(),
        connection_id=conn.id,
        remote_agent_id=agent.id,
        initiator_class=req.initiator_class[:16],
        initiator_id=req.initiator_id,
        correlation_id=req.correlation_id,
        causation_id=req.causation_id,
        intent_id=req.intent_id,
        depth=req.depth,
        operation="SendMessage",
        skill_id=req.skill_id[:128],
        idempotency_key=req.idempotency_key[:255],
        status="pending",
        result_summary={"request": {"input": req.input}},
    )
    db.add(call)
    db.flush()
    return call


def begin(db: Session, req: OutboundRequest) -> A2AOutboundCall:
    call = prepare(db, req)
    db.commit()
    db.refresh(call)
    return call


def _claim(db_factory: Callable[[], Session], call_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """pending -> sent, atomically. Exactly one process wins; a crash after the
    claim leaves the row ``sent`` without a remote id, which is NEVER resent."""
    db = db_factory()
    try:
        call = (
            db.query(A2AOutboundCall)
            .filter(A2AOutboundCall.id == call_id, A2AOutboundCall.status == "pending")
            .with_for_update(skip_locked=True)
            .first()
        )
        if call is None:
            db.rollback()
            return None
        call.status = "sent"
        request = dict((call.result_summary or {}).get("request") or {})
        claimed = {"skill_id": call.skill_id, "depth": int(call.depth or 1), "input": request.get("input") or {}}
        db.commit()
        return claimed
    finally:
        db.close()


def _summarize(obj: Any) -> Dict[str, Any]:
    """Bounded, labelled summary of remote output (UNTRUSTED EXTERNAL DATA)."""
    out: Dict[str, Any] = {"untrusted": True, "source": "remote_a2a_agent"}
    if isinstance(obj, pb.Task):
        out["state"] = pb.TaskState.Name(obj.status.state)
        texts = []
        for artifact in list(obj.artifacts)[:4]:
            for part in list(artifact.parts)[:4]:
                if part.WhichOneof("content") == "text":
                    texts.append(part.text)
                elif part.WhichOneof("content") == "data":
                    texts.append(str(MessageToDict(part.data))[:MAX_SUMMARY_TEXT])
        out["artifactText"] = "\n".join(texts)[:MAX_SUMMARY_TEXT]
        out["artifactCount"] = len(obj.artifacts)
    elif isinstance(obj, pb.Message):
        out["state"] = "MESSAGE"
        out["messageText"] = "\n".join(p.text for p in obj.parts if p.WhichOneof("content") == "text")[:MAX_SUMMARY_TEXT]
    return out


async def _client_for(db_factory: Callable[[], Session], call: A2AOutboundCall):
    db = db_factory()
    try:
        conn = db.query(A2AConnection).filter(A2AConnection.id == call.connection_id).first()
        agent = db.query(A2ARemoteAgent).filter(A2ARemoteAgent.id == call.remote_agent_id).first()
        if conn is None or agent is None or conn.revoked_at is not None or agent.state != "verified":
            raise OutboundRefusedByPolicy("connection_unavailable")
        card = _card_for(agent)
        headers = {}
        if conn.auth_scheme == "bearer":
            secret = vault.unseal(conn.sealed_credential)  # opened here, never stored elsewhere
            if not secret:
                raise OutboundRefusedByPolicy("credential_unavailable")
            headers["Authorization"] = f"Bearer {secret}"
            del secret
    finally:
        db.close()
    http = safe_client(timeout=SEND_TIMEOUT_SECONDS, max_response_bytes=MAX_RESPONSE_BYTES, headers=headers)
    factory = ClientFactory(ClientConfig(httpx_client=http, streaming=False, supported_protocol_bindings=["JSONRPC", "HTTP+JSON"]))
    return http, factory.create(card)


def _finish(db_factory: Callable[[], Session], call_id: uuid.UUID, **fields: Any) -> Dict[str, Any]:
    db = db_factory()
    try:
        call = db.query(A2AOutboundCall).filter(A2AOutboundCall.id == call_id).with_for_update().one()
        for key, value in fields.items():
            setattr(call, key, value)
        db.commit()
        db.refresh(call)
        return call_view(call)
    finally:
        db.close()


def _error_class(exc: Exception) -> str:
    if isinstance(exc, (OutboundRefused, OutboundRefusedByPolicy)):
        return f"refused:{exc.reason}"[:64]
    return type(exc).__name__[:64]


async def send(db_factory: Callable[[], Session], req: OutboundRequest) -> Dict[str, Any]:
    """Record (or reuse) the call, then deliver it once. Returns the call view."""
    db = db_factory()
    try:
        call = begin(db, req)
        call_id = call.id
        if call.status != "pending":
            return call_view(call)  # idempotent replay: never send twice
    finally:
        db.close()
    return await deliver(db_factory, call_id)


async def deliver(db_factory: Callable[[], Session], call_id: uuid.UUID) -> Dict[str, Any]:
    """Send ONE pending call (never retried). Safe to call from several
    processes: only the one that claims the row sends."""
    claimed = _claim(db_factory, call_id)
    if claimed is None:
        db = db_factory()
        try:
            row = db.query(A2AOutboundCall).filter(A2AOutboundCall.id == call_id).first()
            if row is None:
                raise KeyError("outbound call not found")
            return call_view(row)
        finally:
            db.close()
    payload = claimed["input"]
    if set(payload) == {"text"} and isinstance(payload["text"], str):
        part = pb.Part(text=payload["text"], media_type="text/plain")  # text-only skills (e.g. the reference agent)
    else:
        value = Value()
        value.struct_value.update(payload)
        part = pb.Part(data=value, media_type="application/json")
    message = pb.Message(message_id=str(call_id), role=pb.ROLE_USER, parts=[part])
    message.metadata.update({"skillId": claimed["skill_id"], config.FEDERATION_EXTENSION_URI: {"depth": claimed["depth"]}})
    request = pb.SendMessageRequest(message=message, configuration=pb.SendMessageConfiguration(return_immediately=True))
    http = None
    db = db_factory()
    try:
        call = db.query(A2AOutboundCall).filter(A2AOutboundCall.id == call_id).one()
        db.expunge(call)
    finally:
        db.close()
    request_record = {"input": payload}
    try:
        http, client = await _client_for(db_factory, call)
        result = None
        async for event in client.send_message(request):
            result = event.task if event.HasField("task") else event.message
            break
        if result is None:
            raise RuntimeError("empty response")
        summary = _summarize(result)
        state = summary.get("state")
        fields: Dict[str, Any] = {"result_summary": {"request": request_record, **summary}, "remote_state": state}
        if isinstance(result, pb.Task):
            fields.update(remote_task_id=result.id[:255], remote_context_id=result.context_id[:255])
            terminal = state in TERMINAL
            fields["status"] = ("succeeded" if state == "TASK_STATE_COMPLETED" else "failed") if terminal else "sent"
        else:
            fields["status"] = "succeeded"
        if fields["status"] != "sent":
            fields["finished_at"] = datetime.now(timezone.utc)
        # the send itself succeeded unless the remote task already failed
        metrics.record_outbound("failed" if fields["status"] == "failed" else "succeeded")
        return _finish(db_factory, call_id, **fields)
    except asyncio.TimeoutError as exc:
        metrics.record_outbound("timeout")
        return _finish(db_factory, call_id, status="timeout", error_class=_error_class(exc), finished_at=datetime.now(timezone.utc))
    except (OutboundRefused, OutboundRefusedByPolicy) as exc:
        metrics.record_outbound("refused")
        return _finish(db_factory, call_id, status="refused", error_class=_error_class(exc), finished_at=datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - recorded, not re-raised: the call row is the outcome
        logger.warning("outbound A2A send failed (%s)", type(exc).__name__)
        metrics.record_outbound("failed")
        return _finish(db_factory, call_id, status="failed", error_class=_error_class(exc), finished_at=datetime.now(timezone.utc))
    finally:
        if http is not None:
            await http.aclose()


async def check(db_factory: Callable[[], Session], call_id: uuid.UUID) -> Dict[str, Any]:
    """GetTask for a sent call (a read: one retry allowed)."""
    db = db_factory()
    try:
        call = db.query(A2AOutboundCall).filter(A2AOutboundCall.id == call_id).first()
        if call is None:
            raise KeyError("outbound call not found")
        if call.status != "sent" or not call.remote_task_id:
            return call_view(call)
        db.expunge(call)
    finally:
        db.close()
    if not config.federation_enabled():
        raise OutboundRefusedByPolicy("federation_disabled")
    last: Optional[Exception] = None
    for _attempt in range(2):
        http = None
        try:
            http, client = await _client_for(db_factory, call)
            task = await client.get_task(pb.GetTaskRequest(id=call.remote_task_id))
            summary = _summarize(task)
            state = summary["state"]
            fields: Dict[str, Any] = {"result_summary": {"request": (call.result_summary or {}).get("request"), **summary}, "remote_state": state}
            if state in TERMINAL:
                fields["status"] = "succeeded" if state == "TASK_STATE_COMPLETED" else "failed"
                fields["finished_at"] = datetime.now(timezone.utc)
            return _finish(db_factory, call.id, **fields)
        except (OutboundRefused, OutboundRefusedByPolicy):
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            await asyncio.sleep(0.5)
        finally:
            if http is not None:
                await http.aclose()
    return _finish(db_factory, call.id, error_class=_error_class(last) if last else None)
