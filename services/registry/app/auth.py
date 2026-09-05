import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import ed25519
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import ValidationError
from sqlalchemy.orm import Session

from .database import get_db
from .models import Agent, User, ScopedToken
from .schemas import AgentToken, TokenData, UserToken

# Environment variables — loaded from app.config which fails fast in non-dev
from .config import JWT_ALGORITHM, JWT_EXPIRATION, JWT_SECRET_KEY  # noqa: E402

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# OAuth2 scheme for user authentication
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/user/login")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a hash. Accounts without a usable hash
    (placeholder/system users) can never log in — that is a clean 401, not
    a 500 from passlib's UnknownHashError."""
    if not plain_password or not hashed_password:
        return False
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except (ValueError, TypeError):
        return False


def get_password_hash(password: str) -> str:
    """Hash a password."""
    return pwd_context.hash(password)


def verify_agent_signature(agent_id: str, signature: str, timestamp: str, public_key: str) -> bool:
    """Verify an agent's Ed25519 signature."""
    try:
        # Reconstruct the message that was signed
        message = f"{agent_id}:{timestamp}"

        # Decode the public key and signature from base64
        public_key_bytes = base64.b64decode(public_key)
        signature_bytes = base64.b64decode(signature)

        # Create a verifying key from the public key
        verifying_key = ed25519.VerifyingKey(public_key_bytes)

        # Verify the signature
        verifying_key.verify(signature_bytes, message.encode())
        return True
    except Exception:
        return False


def create_user_token(user_id: uuid.UUID) -> UserToken:
    """Create a JWT token for a user."""
    to_encode = {"sub": str(user_id), "type": "user"}
    expires_delta = timedelta(seconds=JWT_EXPIRATION)

    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)

    return UserToken(access_token=encoded_jwt, token_type="bearer", expires_in=JWT_EXPIRATION)


def create_agent_token(agent_id: uuid.UUID) -> AgentToken:
    """Create a JWT token for an agent."""
    to_encode = {"sub": str(agent_id), "type": "agent"}
    expires_delta = timedelta(seconds=JWT_EXPIRATION)

    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)

    return AgentToken(access_token=encoded_jwt, token_type="bearer", expires_in=JWT_EXPIRATION)


def verify_token(token: str, db: Optional[Session] = None) -> TokenData:
    """Verify a JWT token or scoped token (spt_) and return token data.

    Two token types:
    - JWT: standard user/agent Bearer tokens (existing behavior)
    - spt_: scoped API tokens with resource limits (new)
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # ── Scoped token (spt_ prefix) ──
    if token.startswith("spt_"):
        if db is None:
            raise credentials_exception
        token_hash = _hash_scoped_token(token)
        spt = db.query(ScopedToken).filter(
            ScopedToken.token_hash == token_hash,
            ScopedToken.is_revoked == False,
        ).first()
        if not spt:
            raise credentials_exception
        if spt.expires_at and spt.expires_at < datetime.now(timezone.utc):
            raise credentials_exception
        return TokenData(
            agent_id=spt.agent_id,
            scoped_token_id=spt.id,
            allowed_actions=spt.allowed_actions or [],
            spending_cap=spt.spending_cap,
            resource_type=spt.resource_type,
            resource_id=spt.resource_id,
        )

    # ── JWT token (existing logic, unchanged) ──
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        id: str = payload.get("sub")
        token_type: str = payload.get("type")

        if id is None or token_type is None:
            raise credentials_exception

        if token_type == "user":
            token_data = TokenData(user_id=uuid.UUID(id))
        elif token_type == "agent":
            token_data = TokenData(agent_id=uuid.UUID(id))
        else:
            raise credentials_exception

        return token_data
    except (JWTError, ValidationError):
        raise credentials_exception


SCOPED_TOKEN_NOT_A_USER = "scoped tokens are agent-scoped and cannot act as a user"


def _attach_scope(db: Session, agent: Agent, token_data: TokenData) -> Agent:
    """Remember the scoped token a request came in with (transient attribute,
    never persisted) so money paths can enforce allowed_actions/spending_cap."""
    spt = None
    if token_data.scoped_token_id is not None:
        spt = db.query(ScopedToken).filter(ScopedToken.id == token_data.scoped_token_id).first()
    setattr(agent, "scoped_token", spt)
    return agent


async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    """Get the current user from a USER JWT.

    Scoped ``spt_`` tokens are refused here: they are minted per agent and
    must never resolve to the owning user's full authority (that would let
    anyone holding an agent token manage the owner's account, wallets and
    every other agent)."""
    token_data = verify_token(token, db=db)

    if token_data.scoped_token_id is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=SCOPED_TOKEN_NOT_A_USER)

    if token_data.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.query(User).filter(User.id == token_data.user_id).first()

    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return user


async def get_current_agent(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> Agent:
    """Get the current agent from a JWT token."""
    token_data = verify_token(token, db=db)

    if token_data.agent_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    agent = db.query(Agent).filter(Agent.id == token_data.agent_id).first()

    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    return _attach_scope(db, agent, token_data)


async def get_current_user_or_agent(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> Union[User, Agent]:
    """Get the current user or agent from a JWT or scoped token."""
    token_data = verify_token(token, db=db)

    # Scoped token: return agent (scoped tokens are agent-scoped)
    if token_data.scoped_token_id is not None and token_data.agent_id is not None:
        agent = db.query(Agent).filter(Agent.id == token_data.agent_id).first()
        if agent is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
        return _attach_scope(db, agent, token_data)

    if token_data.user_id is not None:
        user = db.query(User).filter(User.id == token_data.user_id).first()
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        return user
    elif token_data.agent_id is not None:
        agent = db.query(Agent).filter(Agent.id == token_data.agent_id).first()
        if agent is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
        return agent
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


AGENT_LOGIN_MAX_SKEW_SECONDS = int(os.getenv("AGENT_LOGIN_MAX_SKEW_SECONDS", "300"))


def _parse_login_timestamp(timestamp: str) -> Optional[datetime]:
    """Accept a unix epoch (seconds, optionally fractional) or ISO-8601."""
    ts = (timestamp or "").strip()
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def login_timestamp_is_fresh(timestamp: str, *, now: Optional[datetime] = None) -> bool:
    """A signed login is only valid for a short window around *now*; otherwise a
    captured signature could be replayed forever (the signed message is
    ``agent_id:timestamp`` and carries no nonce)."""
    parsed = _parse_login_timestamp(timestamp)
    if parsed is None:
        return False
    now = now or datetime.now(timezone.utc)
    return abs((now - parsed).total_seconds()) <= AGENT_LOGIN_MAX_SKEW_SECONDS


def get_agent_by_signature(agent_id: str, signature: str, timestamp: str, db: Session) -> Optional[Agent]:
    """Get an agent by ID and verify its signature (fresh timestamp required)."""
    if not login_timestamp_is_fresh(timestamp):
        return None

    agent = db.query(Agent).filter(Agent.id == agent_id).first()

    if agent is None:
        return None

    # Verify the signature
    if not verify_agent_signature(agent_id, signature, timestamp, agent.public_key):
        return None

    return agent


def hash_input(data: dict) -> str:
    """Hash input data for audit purposes."""
    # Sort keys to ensure consistent hashing
    sorted_data = json.dumps(data, sort_keys=True)
    return hashlib.sha256(sorted_data.encode()).hexdigest()


def _hash_scoped_token(raw: str) -> str:
    """Hash a scoped token (spt_ prefix) using SHA-256.

    Mirrors tokens.py:_hash_token. Used by verify_token() when
    the Bearer token has the 'spt_' prefix.
    """
    return hashlib.sha256(raw.encode()).hexdigest()
