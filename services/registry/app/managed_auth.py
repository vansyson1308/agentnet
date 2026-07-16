"""Narrow authentication helpers for managed services and runtimes."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import MANAGED_EXECUTION_ENABLED, MANAGED_EXECUTION_SERVICE_TOKEN, RUNTIME_REGISTRATION_TOKEN
from .database import get_db
from .managed_models import Runtime

bearer = HTTPBearer(auto_error=False)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_runtime_token() -> str:
    return "rt_" + secrets.token_urlsafe(32)


def new_lease_token() -> str:
    return "lease_" + secrets.token_urlsafe(32)


def _require_static_token(
    credentials: HTTPAuthorizationCredentials | None,
    expected: str,
) -> None:
    if not MANAGED_EXECUTION_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="managed execution is disabled")
    if credentials is None or not hmac.compare_digest(credentials.credentials, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid service credential")


def require_managed_service(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> None:
    _require_static_token(credentials, MANAGED_EXECUTION_SERVICE_TOKEN)


def require_runtime_registrar(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> None:
    _require_static_token(credentials, RUNTIME_REGISTRATION_TOKEN)


def get_current_runtime(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> Runtime:
    if not MANAGED_EXECUTION_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="managed execution is disabled")
    if credentials is None or not credentials.credentials.startswith("rt_"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid runtime credential")
    runtime = db.query(Runtime).filter(Runtime.token_hash == hash_secret(credentials.credentials)).first()
    if runtime is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid runtime credential")
    return runtime
