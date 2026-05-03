"""
Worker configuration with fail-fast secret validation.

Mirrors `services/registry/app/config.py` — see that file for the rationale.
The worker doesn't issue JWTs but does talk to Redis with a password, so we
still validate REDIS_PASSWORD via require_env.
"""

import hashlib
import os
from typing import Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
IS_DEV = ENVIRONMENT == "development"


def _dev_only_default(name: str) -> str:
    """Stable, hash-derived dev fallback (see registry/app/config.py)."""
    digest = hashlib.sha256(f"agentnet-dev/{name}".encode("utf-8")).hexdigest()
    return "dev-only-" + digest[:48]


_DEV_REDIS_PASSWORD = _dev_only_default("redis-password")
_DEV_POSTGRES_PASSWORD = _dev_only_default("postgres-password")

_LEFTOVER_DEFAULTS = frozenset(
    {
        "your_jwt_secret_key",
        "your_redis_password",
        "your_secure_password",
        "your_db_password",
        "your_postgres_password",
        "changeme",
        "change_me",
        "",
    }
)


def require_env(name: str, *, dev_default: Optional[str] = None) -> str:
    """See ``services/registry/app/config.py:require_env``."""
    val = os.getenv(name, "")
    if IS_DEV:
        if val:
            return val
        if dev_default is not None:
            return dev_default
        raise RuntimeError(f"Required environment variable {name!r} is not set.")
    if not val:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set."
        )
    if val in _LEFTOVER_DEFAULTS:
        raise RuntimeError(
            f"Environment variable {name!r} is set to a known placeholder "
            f"value ({val!r}). Refusing to start in {ENVIRONMENT!r}."
        )
    return val


REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_PASSWORD = require_env("REDIS_PASSWORD", dev_default=_DEV_REDIS_PASSWORD)
REDIS_URL = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/0"

POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_USER = os.getenv("POSTGRES_USER", "agentnet")
POSTGRES_DB = os.getenv("POSTGRES_DB", "agentnet")
POSTGRES_PASSWORD = require_env(
    "POSTGRES_PASSWORD", dev_default=_DEV_POSTGRES_PASSWORD
)
DATABASE_URL = (
    f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
    f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
)
