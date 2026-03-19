"""
Authentication for the simulation service.

Uses the same JWT shared secret as registry/payment services.
Supports both user and agent tokens.
"""

import os
import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from pydantic import BaseModel, ValidationError

# Shared JWT config (same secret across all services)
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your_jwt_secret_key")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/user/login", auto_error=False)


class TokenData(BaseModel):
    user_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None


def verify_token(token: str) -> TokenData:
    """Verify a JWT token and return the token data."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        sub: str = payload.get("sub")
        token_type: str = payload.get("type")
        if sub is None or token_type is None:
            raise credentials_exception

        if token_type == "user":
            return TokenData(user_id=uuid.UUID(sub))
        elif token_type == "agent":
            return TokenData(agent_id=uuid.UUID(sub))
        else:
            raise credentials_exception
    except (JWTError, ValidationError, ValueError):
        raise credentials_exception


async def get_current_user_id(token: str = Depends(oauth2_scheme)) -> uuid.UUID:
    """Extract user ID from JWT token. Requires a user token."""
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token_data = verify_token(token)
    if token_data.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only user tokens are accepted for simulation operations",
        )
    return token_data.user_id


async def get_current_user_or_agent_id(
    token: str = Depends(oauth2_scheme),
) -> tuple[str, uuid.UUID]:
    """Extract (type, id) from JWT token. Returns ('user', id) or ('agent', id)."""
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token_data = verify_token(token)
    if token_data.user_id:
        return ("user", token_data.user_id)
    elif token_data.agent_id:
        return ("agent", token_data.agent_id)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid token",
    )
