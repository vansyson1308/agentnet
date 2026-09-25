"""Sealed credentials for outbound A2A connections (ADR-0009 D12).

A connection's bearer credential is stored only as a Fernet token keyed by
``A2A_CREDENTIAL_KEY``. It is unsealed only inside the outbound client,
immediately before the request, and is never logged, returned by an API,
put in a prompt or Society context, or forwarded to another agent.
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

MIN_SECRET_CHARS = 32


class VaultUnavailable(Exception):
    """No valid key is configured, so credentials can be neither sealed nor used."""


def _fernet() -> Fernet:
    """A Fernet key, or any high-entropy secret of at least 32 characters
    (e.g. a Railway shared variable generated with ``${{secret(64, ...)}}``) from which the key is
    derived with SHA-256 -- so the operator never has to see or paste one."""
    key = os.getenv("A2A_CREDENTIAL_KEY", "").strip()
    if not key:
        raise VaultUnavailable("A2A_CREDENTIAL_KEY is not configured")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        pass
    if len(key) < MIN_SECRET_CHARS:
        raise VaultUnavailable(f"A2A_CREDENTIAL_KEY must be a Fernet key or a secret of at least {MIN_SECRET_CHARS} characters")
    derived = base64.urlsafe_b64encode(hashlib.sha256(b"agentnet-a2a-vault-v1:" + key.encode()).digest())
    return Fernet(derived)


def available() -> bool:
    try:
        _fernet()
        return True
    except VaultUnavailable:
        return False


def seal(secret: str) -> str:
    if not secret or len(secret) > 8192:
        raise ValueError("credential must be 1..8192 characters")
    return _fernet().encrypt(secret.encode()).decode()


def unseal(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise VaultUnavailable("the sealed credential cannot be opened with the current key") from exc
