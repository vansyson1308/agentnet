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
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ...database import get_db
from ...models import ScopedToken
from ...schemas import (
    ScopedTokenCreate,
    ScopedTokenResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@router.post("/tokens", response_model=ScopedTokenResponse, status_code=201)
async def create_scoped_token(body: ScopedTokenCreate, db: Session = Depends(get_db)):
    """Create a scoped API token for a specific resource + agent.

    The token is only valid for the specified resource (e.g. domain X, bucket Y).
    It carries spending_cap, expiry, and allowed_actions.

    Returns the raw token ONCE — it is not stored plaintext.
    """
    raw = "spt_" + secrets.token_urlsafe(32)
    token_hash = _hash_token(raw)

    now = datetime.now(timezone.utc)
    expires_at = now + body.expires_in if body.expires_in else None

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
async def get_scoped_token(token_id: uuid.UUID, db: Session = Depends(get_db)):
    token = db.query(ScopedToken).filter(ScopedToken.id == token_id).first()
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")
    return token


@router.delete("/tokens/{token_id}", status_code=204)
async def revoke_scoped_token(token_id: uuid.UUID, db: Session = Depends(get_db)):
    token = db.query(ScopedToken).filter(ScopedToken.id == token_id).first()
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")
    token.is_revoked = True
    token.revoked_at = datetime.now(timezone.utc)
    db.commit()
    return None
