import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta
from typing import Optional, Union

import ed25519
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import ValidationError
from sqlalchemy.orm import Session

from .database import get_db
from .models import Agent, User
from .schemas import AgentToken, TokenData, UserToken

# Environment variables
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your_jwt_secret_key")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRATION = int(os.getenv("JWT_EXPIRATION", "3600"))  # 1 hour

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# OAuth2 scheme for user authentication
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/user/login")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a hash."""
    return pwd_context.verify(plain_password, hashed_password)


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


def verify_token(token: str) -> TokenData:
    """Verify a JWT token and return the token data."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

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


async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    """Get the current user from a JWT token."""
    token_data = verify_token(token)

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
    token_data = verify_token(token)

    if token_data.agent_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    agent = db.query(Agent).filter(Agent.id == token_data.agent_id).first()

    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    return agent


async def get_current_user_or_agent(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> Union[User, Agent]:
    """Get the current user or agent from a JWT token."""
    token_data = verify_token(token)

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


def get_agent_by_signature(agent_id: str, signature: str, timestamp: str, db: Session) -> Optional[Agent]:
    """Get an agent by ID and verify its signature."""
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
