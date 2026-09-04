"""Scoped API Token system (AB-416).

Mirrors Stripe Shared Payment Token + Cloudflare scoped token.

POST /v1/tokens — create scoped token for specific resource + agent
GET /v1/tokens/{id} — verify token status
DELETE /v1/tokens/{id} — revoke token
"""

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...authz import require_owned_agent
from ...database import get_db
from ...models import Agent, ScopedToken, User
from ...schemas import (
    ScopedTokenCreate,
    ScopedTokenResponse,
)

# Tokens are bearer credentials: bound by default, capped at one year.
DEFAULT_TOKEN_TTL_SECONDS = 30 * 24 * 3600
MAX_TOKEN_TTL_SECONDS = 365 * 24 * 3600

logger = logging.getLogger(__name__)
router = APIRouter()


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _owned_token(db: Session, token_id: uuid.UUID, user: User) -> ScopedToken:
    token = db.query(ScopedToken).filter(ScopedToken.id == token_id).first()
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")
    owner = db.query(Agent.id).filter(Agent.id == token.agent_id, Agent.user_id == user.id).first()
    if owner is None:
        # Do not reveal that the token exists to a non-owner.
        raise HTTPException(status_code=404, detail="Token not found")
    return token


@router.post("/tokens", response_model=ScopedTokenResponse, status_code=201)
async def create_scoped_token(body: ScopedTokenCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Create a scoped API token for one of YOUR agents.

    The token is only valid for the specified resource (e.g. domain X, bucket Y).
    It carries spending_cap, expiry, and allowed_actions. Only the agent's
    owner (a user session) may mint it; agent JWTs and other scoped tokens
    cannot. Expiry defaults to 30 days and is capped at one year.

    Returns the raw token ONCE — it is not stored plaintext.
    """
    require_owned_agent(db, current_user, body.agent_id, detail="you can only mint tokens for agents you own")
    if body.spending_cap is not None and body.spending_cap < 0:
        raise HTTPException(status_code=422, detail="spending_cap must be >= 0")
    ttl = body.expires_in if body.expires_in is not None else DEFAULT_TOKEN_TTL_SECONDS
    if ttl <= 0 or ttl > MAX_TOKEN_TTL_SECONDS:
        raise HTTPException(status_code=422, detail=f"expires_in must be 1..{MAX_TOKEN_TTL_SECONDS} seconds")

    raw = "spt_" + secrets.token_urlsafe(32)
    token_hash = _hash_token(raw)

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=ttl)

    token = ScopedToken(
        token_hash=token_hash,
        agent_id=body.agent_id,
        resource_type=body.resource_type,
        resource_id=str(body.resource_id) if body.resource_id else None,
        spending_cap=body.spending_cap,
        allowed_actions=body.allowed_actions,
        expires_at=expires_at,
        project_id=body.project_id,
    )
    db.add(token)
    db.commit()
    db.refresh(token)

    resp = ScopedTokenResponse.model_validate(token)
    resp.raw_token = raw  # show once
    return resp


@router.get("/tokens/{token_id}", response_model=ScopedTokenResponse)
async def get_scoped_token(token_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return _owned_token(db, token_id, current_user)


@router.delete("/tokens/{token_id}", status_code=204)
async def revoke_scoped_token(token_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    token = _owned_token(db, token_id, current_user)
    token.is_revoked = True
    token.revoked_at = datetime.now(timezone.utc)
    db.commit()
    return None
