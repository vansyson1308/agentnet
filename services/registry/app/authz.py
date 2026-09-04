"""Resource-ownership and scoped-token guards shared by the registry routes.

Rules (see docs/DEPLOYMENT_ARCHITECTURE.md §Authorization):

* A *principal* is either a ``User`` (user JWT) or an ``Agent`` (agent JWT or
  scoped ``spt_`` token). Users act for every agent they own; agents act only
  for themselves.
* Knowing a UUID never grants access: every mutating route checks that the
  target belongs to the principal, and reads of private data are scoped the
  same way.
* Scoped tokens carry ``allowed_actions`` and ``spending_cap``. Money-moving
  operations require the ``execute`` action and are charged against the cap
  atomically (row lock on the token) in the same transaction as the escrow.
* Platform-level governance (approving improvement proposals, society-scope
  goals/memory, orchestrator partners) needs the society ``operator`` role —
  the only privilege tier the platform has.
"""

from __future__ import annotations

import uuid
from typing import Iterable, Optional, Set, Union

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from .models import Agent, ScopedToken, User
from .society.operator_auth import is_operator

Principal = Union[User, Agent]

ACTION_EXECUTE = "execute"


def principal_agent_ids(db: Session, principal: Principal) -> Set[uuid.UUID]:
    """Agents the principal may act for."""
    if isinstance(principal, Agent):
        return {principal.id}
    return {row[0] for row in db.query(Agent.id).filter(Agent.user_id == principal.id).all()}


def owns_agent(db: Session, principal: Principal, agent_id: uuid.UUID) -> bool:
    if isinstance(principal, Agent):
        return principal.id == agent_id
    return db.query(Agent.id).filter(Agent.id == agent_id, Agent.user_id == principal.id).first() is not None


def require_owned_agent(db: Session, principal: Principal, agent_id: uuid.UUID, *, detail: str = "you do not own this agent") -> Agent:
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if not owns_agent(db, principal, agent_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
    return agent


def require_party(db: Session, principal: Principal, agent_ids: Iterable[Optional[uuid.UUID]], *, detail: str = "not a party to this resource") -> None:
    """The principal must own at least one of ``agent_ids``."""
    mine = principal_agent_ids(db, principal)
    if not any(a is not None and a in mine for a in agent_ids):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def user_is_operator(principal: Principal) -> bool:
    return isinstance(principal, User) and is_operator(principal)


def require_operator_user(principal: Principal, *, detail: str = "society operator role required") -> User:
    if not user_is_operator(principal):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
    return principal  # type: ignore[return-value]


# ── scoped tokens ──────────────────────────────────────────────────────────


def scoped_token_of(principal: Principal) -> Optional[ScopedToken]:
    return getattr(principal, "scoped_token", None) if isinstance(principal, Agent) else None


def require_scoped_action(principal: Principal, action: str) -> None:
    """No-op for JWT principals; a scoped token must list ``action``."""
    spt = scoped_token_of(principal)
    if spt is None:
        return
    if action not in set(spt.allowed_actions or []):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"scoped token does not allow action {action!r}")


def reserve_scoped_spend(db: Session, principal: Principal, amount: int) -> None:
    """Charge ``amount`` against a scoped token's spending cap under a row lock.

    Runs inside the caller's transaction so a later escrow failure rolls the
    charge back with it; a concurrent request on the same token waits on the
    lock and sees the updated total (no double spend past the cap)."""
    spt = scoped_token_of(principal)
    if spt is None:
        return
    require_scoped_action(principal, ACTION_EXECUTE)
    locked = db.query(ScopedToken).filter(ScopedToken.id == spt.id).with_for_update().first()
    if locked is None or locked.is_revoked:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="scoped token revoked")
    spent = int(locked.total_spent or 0)
    cap = int(locked.spending_cap or 0)
    if spent + int(amount) > cap:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"scoped token spending cap exceeded ({spent}+{amount} > {cap})")
    locked.total_spent = spent + int(amount)


def scoped_action_allowed_by_id(db: Session, token_id: uuid.UUID, action: str) -> bool:
    spt = db.query(ScopedToken).filter(ScopedToken.id == token_id).first()
    return bool(spt) and not spt.is_revoked and action in set(spt.allowed_actions or [])


def reserve_scoped_spend_by_id(db: Session, token_id: uuid.UUID, amount: int) -> None:
    """WebSocket variant of :func:`reserve_scoped_spend`; raises PermissionError."""
    locked = db.query(ScopedToken).filter(ScopedToken.id == token_id).with_for_update().first()
    if locked is None or locked.is_revoked:
        raise PermissionError("scoped token revoked")
    if ACTION_EXECUTE not in set(locked.allowed_actions or []):
        raise PermissionError("scoped token does not allow action 'execute'")
    spent = int(locked.total_spent or 0)
    cap = int(locked.spending_cap or 0)
    if spent + int(amount) > cap:
        raise PermissionError(f"scoped token spending cap exceeded ({spent}+{amount} > {cap})")
    locked.total_spent = spent + int(amount)
