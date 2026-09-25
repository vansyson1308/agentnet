"""A2A authentication: the registry's own credentials, nothing new (ADR-0009 D6).

``Authorization: Bearer`` carries a user JWT, an agent JWT or an agent-scoped
``spt_`` token, verified by ``app.auth.verify_token`` (signature, expiry, the
scoped-token hash lookup and revocation). A2A adds no second identity system,
no API keys and no query-string credentials.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import FrozenSet, Optional

from a2a.auth.user import User as A2AUserBase
from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..auth import _attach_scope, verify_token
from ..models import Agent, ScopedToken, User


class AuthenticationFailed(Exception):
    """No valid AgentNet credential. Answered with HTTP 401 before any A2A work."""


@dataclass(frozen=True)
class A2APrincipal:
    """Who is calling, resolved once per request.

    ``agent_ids`` are the agents this principal acts for: itself for an agent
    credential, every owned agent for a user (``authz.principal_agent_ids``)."""

    kind: str  # "agent" | "user"
    agent_id: Optional[uuid.UUID]
    user_id: Optional[uuid.UUID]
    scoped_token_id: Optional[uuid.UUID]
    agent_ids: FrozenSet[uuid.UUID]

    @property
    def key(self) -> str:
        """Stable, non-secret identity used for idempotency and stream bounds."""
        return f"{self.kind}:{self.agent_id or self.user_id}"

    @property
    def principal_id(self) -> uuid.UUID:
        return self.agent_id if self.kind == "agent" else self.user_id  # type: ignore[return-value]


def bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or len(token) > 4096:
        return None
    return token


def resolve_principal(db: Session, authorization: Optional[str]) -> A2APrincipal:
    """Verify the bearer credential; raise :class:`AuthenticationFailed` otherwise."""
    token = bearer_token(authorization)
    if token is None:
        raise AuthenticationFailed()
    try:
        data = verify_token(token, db=db)
    except HTTPException as exc:
        raise AuthenticationFailed() from exc

    if data.agent_id is not None:
        agent = db.query(Agent).filter(Agent.id == data.agent_id).first()
        if agent is None:
            raise AuthenticationFailed()
        return A2APrincipal(
            kind="agent",
            agent_id=agent.id,
            user_id=None,
            scoped_token_id=data.scoped_token_id,
            agent_ids=frozenset({agent.id}),
        )
    if data.user_id is not None:
        user = db.query(User).filter(User.id == data.user_id).first()
        if user is None:
            raise AuthenticationFailed()
        owned = frozenset(row[0] for row in db.query(Agent.id).filter(Agent.user_id == user.id).all())
        return A2APrincipal(kind="user", agent_id=None, user_id=user.id, scoped_token_id=None, agent_ids=owned)
    raise AuthenticationFailed()


def load_paying_agent(db: Session, principal: A2APrincipal) -> Agent:
    """The ORM agent that pays for a task, with its scoped token attached so
    ``authz.reserve_scoped_spend`` enforces ``execute`` and the spending cap in
    the escrow transaction (the same object shape the REST route uses)."""
    if principal.kind != "agent" or principal.agent_id is None:
        raise AuthenticationFailed()
    agent = db.query(Agent).filter(Agent.id == principal.agent_id).first()
    if agent is None:
        raise AuthenticationFailed()

    class _TokenData:
        scoped_token_id = principal.scoped_token_id

    return _attach_scope(db, agent, _TokenData())  # type: ignore[arg-type]


def scoped_token_allows(db: Session, principal: A2APrincipal, action: str) -> bool:
    if principal.scoped_token_id is None:
        return True
    spt = db.query(ScopedToken).filter(ScopedToken.id == principal.scoped_token_id).first()
    return bool(spt) and not spt.is_revoked and action in set(spt.allowed_actions or [])


class A2AUser(A2AUserBase):
    """The SDK's user view of a principal (``ServerCallContext.user``)."""

    def __init__(self, principal: Optional[A2APrincipal]):
        self._principal = principal

    @property
    def is_authenticated(self) -> bool:
        return self._principal is not None

    @property
    def user_name(self) -> str:
        return self._principal.key if self._principal else ""
