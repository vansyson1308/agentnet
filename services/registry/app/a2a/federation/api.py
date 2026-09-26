"""Operator API for the federation catalog and outbound calls (ADR-0009 D12).

Every route requires the society ``operator`` role (``operator_auth``, the
one operator authority, user JWTs only) and ``A2A_FEDERATION_ENABLED``.
Remote content is returned as labelled data. Credentials are write-only:
no response ever contains a credential or its sealed form.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ...database import get_db
from ...models import User
from ...society.operator_auth import require_operator
from .. import config
from ..orm import A2AConnection, A2AOutboundCall, A2ARemoteAgent
from . import catalog, client, vault
from .fetcher import CardFetchError
from .netguard import OutboundRefused

router = APIRouter(prefix="/v1/a2a/federation", tags=["a2a-federation"])


def _session_factory():
    from ..routes import runtime

    return runtime.new_session


def _enabled() -> None:
    if not config.federation_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="A2A federation is disabled")


class DiscoverBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_url: str = Field(alias="cardUrl", min_length=8, max_length=2048)


class StateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: str
    reason: Optional[str] = Field(default=None, max_length=255)


class ConnectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    remote_agent_id: uuid.UUID = Field(alias="remoteAgentId")
    label: str = Field(min_length=1, max_length=128)
    auth_scheme: str = Field(default="none", alias="authScheme")
    credential: Optional[str] = Field(default=None, max_length=8192)
    daily_call_limit: int = Field(default=20, alias="dailyCallLimit", ge=1, le=1000)


class CallBody(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    connection_id: uuid.UUID = Field(alias="connectionId")
    skill_id: str = Field(alias="skillId", min_length=1, max_length=128)
    input: Dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Optional[str] = Field(default=None, alias="idempotencyKey", max_length=128)


def _connection_view(conn: A2AConnection) -> Dict[str, Any]:
    return {
        "id": str(conn.id),
        "remoteAgentId": str(conn.remote_agent_id),
        "label": conn.label,
        "authScheme": conn.auth_scheme,
        "hasCredential": bool(conn.sealed_credential),
        "dailyCallLimit": conn.daily_call_limit,
        "createdAt": conn.created_at.isoformat() if conn.created_at else None,
        "revokedAt": conn.revoked_at.isoformat() if conn.revoked_at else None,
    }


@router.get("/agents")
def list_remote_agents(state: Optional[str] = Query(default=None, max_length=16), db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    _enabled()
    q = db.query(A2ARemoteAgent)
    if state:
        q = q.filter(A2ARemoteAgent.state == state)
    return {"agents": [catalog.public_view(a) for a in q.order_by(A2ARemoteAgent.updated_at.desc()).limit(200).all()]}


@router.post("/agents", status_code=status.HTTP_201_CREATED)
async def discover_remote_agent(body: DiscoverBody, operator: User = Depends(require_operator)):
    _enabled()
    try:
        return await catalog.discover(_session_factory(), body.card_url, {"source": "operator", "by": str(operator.id), "at": datetime.now(timezone.utc).isoformat()})
    except OutboundRefused as exc:
        raise HTTPException(status_code=422, detail=f"destination refused: {exc.reason}")
    except CardFetchError as exc:
        raise HTTPException(status_code=422, detail=f"card rejected: {exc.result}")


@router.post("/agents/{remote_agent_id}/refresh")
async def refresh_remote_agent(remote_agent_id: uuid.UUID, operator: User = Depends(require_operator)):
    _enabled()
    try:
        return await catalog.refresh(_session_factory(), remote_agent_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="remote agent not found")


@router.post("/agents/{remote_agent_id}/state")
def set_remote_agent_state(remote_agent_id: uuid.UUID, body: StateBody, db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    _enabled()
    try:
        return catalog.set_state(db, remote_agent_id, body.state, body.reason, operator.id)
    except KeyError:
        raise HTTPException(status_code=404, detail="remote agent not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/connections")
def list_connections(db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    _enabled()
    rows = db.query(A2AConnection).order_by(A2AConnection.created_at.desc()).limit(200).all()
    return {"connections": [_connection_view(c) for c in rows]}


@router.post("/connections", status_code=status.HTTP_201_CREATED)
def create_connection(body: ConnectionBody, db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    _enabled()
    if body.auth_scheme not in ("none", "bearer"):
        raise HTTPException(status_code=422, detail="authScheme must be none or bearer")
    if db.query(A2ARemoteAgent.id).filter(A2ARemoteAgent.id == body.remote_agent_id).first() is None:
        raise HTTPException(status_code=404, detail="remote agent not found")
    sealed = None
    if body.auth_scheme == "bearer":
        if not body.credential:
            raise HTTPException(status_code=422, detail="a bearer connection needs a credential")
        try:
            sealed = vault.seal(body.credential)
        except vault.VaultUnavailable:
            raise HTTPException(status_code=503, detail="credential vault is not configured (A2A_CREDENTIAL_KEY)")
    elif body.credential:
        raise HTTPException(status_code=422, detail="authScheme none takes no credential")
    conn = A2AConnection(
        id=uuid.uuid4(),
        remote_agent_id=body.remote_agent_id,
        label=body.label,
        auth_scheme=body.auth_scheme,
        sealed_credential=sealed,
        daily_call_limit=body.daily_call_limit,
        created_by_user_id=operator.id,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return _connection_view(conn)


@router.delete("/connections/{connection_id}")
def revoke_connection(connection_id: uuid.UUID, db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    _enabled()
    conn = db.query(A2AConnection).filter(A2AConnection.id == connection_id).with_for_update().first()
    if conn is None:
        raise HTTPException(status_code=404, detail="connection not found")
    if conn.revoked_at is None:
        conn.revoked_at = datetime.now(timezone.utc)
        conn.sealed_credential = None  # revocation destroys the sealed credential
        db.commit()
    return _connection_view(conn)


@router.get("/calls")
def list_calls(db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    _enabled()
    rows = db.query(A2AOutboundCall).order_by(A2AOutboundCall.created_at.desc()).limit(200).all()
    return {"calls": [client.call_view(c) for c in rows]}


@router.post("/calls", status_code=status.HTTP_201_CREATED)
async def send_call(body: CallBody, operator: User = Depends(require_operator)):
    """Operator-initiated outbound call (the Society uses REQUEST_A2A_TASK)."""
    _enabled()
    key = f"operator:{operator.id}:{body.idempotency_key or uuid.uuid4().hex}"
    req = client.OutboundRequest(
        connection_id=body.connection_id,
        skill_id=body.skill_id,
        input=body.input,
        idempotency_key=key,
        initiator_class="operator",
        initiator_id=operator.id,
    )
    try:
        return await client.send(_session_factory(), req)
    except client.OutboundRefusedByPolicy as exc:
        raise HTTPException(status_code=422, detail=f"refused: {exc.reason}")


@router.post("/calls/{call_id}/check")
async def check_call(call_id: uuid.UUID, operator: User = Depends(require_operator)):
    _enabled()
    try:
        return await client.check(_session_factory(), call_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="outbound call not found")
    except (client.OutboundRefusedByPolicy, OutboundRefused) as exc:
        raise HTTPException(status_code=422, detail=f"refused: {exc.reason}")
