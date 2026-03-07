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
from .schemas import TokenData

# Environment variables
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your_jwt_secret_key")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRATION = int(os.getenv("JWT_EXPIRATION", "3600"))  # 1 hour

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# OAuth2 scheme for user authentication
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/user/login")


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
