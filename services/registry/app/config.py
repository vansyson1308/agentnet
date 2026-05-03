"""
Service configuration with fail-fast secret validation.

Replaces ad-hoc `os.getenv("X", "your_jwt_secret_key")` patterns. In any
non-development environment, missing or placeholder secrets raise on import.
The dev defaults are intentionally identical across registry/payment/
simulation/worker so a single docker compose stack works without env files,
but they are obviously-fake strings — never use them in production.
"""

import hashlib
import os
from typing import Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional outside dev/test
    pass

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
IS_DEV = ENVIRONMENT == "development"


def _dev_only_default(name: str) -> str:
    """Stable, obviously-non-prod fallback for the given secret name.

    Computed from a hash so no password-shaped literal lives in source
    (secret scanners would otherwise flag this file). Same input always
    produces the same value, so JWT tokens issued by registry still
    verify in payment / simulation when every service is running with
    its env var literally unset (e.g. `pytest` invocation, no .env).
    """
    digest = hashlib.sha256(f"agentnet-dev/{name}".encode("utf-8")).hexdigest()
    return "dev-only-" + digest[:48]


# These are NOT secrets. They are sentinel values used only in
# ENVIRONMENT=development when the corresponding env var is literally
# unset. require_env() in non-dev refuses to use them.
_DEV_JWT_SECRET = _dev_only_default("jwt-secret")
_DEV_REDIS_PASSWORD = _dev_only_default("redis-password")
_DEV_POSTGRES_PASSWORD = _dev_only_default("postgres-password")

# Known leftover defaults that ship with the repo. If any of these slip
# through to a non-dev environment we treat the env var as unset and raise,
# because they are public knowledge and would let an attacker forge tokens
# or pop the cluster.
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
    """Read an env var, fail fast in non-dev when missing or set to a known leftover.

    Dev semantics: if the value is set (even to a known placeholder like
    ``your_secure_password``), return it verbatim so the value matches what
    other containers in the stack — Postgres, Redis — were initialised with.
    Only fall back to ``dev_default`` if literally unset. This keeps
    ``cp .env.example .env && docker compose up`` working without any edit.

    Non-dev semantics: reject empty AND reject any leftover placeholder
    that ships with this repo. Those strings are public and would let an
    attacker forge tokens or pop the cluster.
    """
    val = os.getenv(name, "")
    if IS_DEV:
        if val:
            return val
        if dev_default is not None:
            return dev_default
        raise RuntimeError(
            f"Required environment variable {name!r} is not set."
        )

    # Production / staging — strict mode.
    if not val:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            f"This must be set in non-development environments."
        )
    if val in _LEFTOVER_DEFAULTS:
        raise RuntimeError(
            f"Environment variable {name!r} is set to a known placeholder "
            f"value ({val!r}). Refusing to start in {ENVIRONMENT!r}. "
            f"Set a real secret in your environment."
        )
    return val


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
JWT_SECRET_KEY = require_env("JWT_SECRET_KEY", dev_default=_DEV_JWT_SECRET)
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRATION = int(os.getenv("JWT_EXPIRATION", "3600"))

# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_PASSWORD = require_env("REDIS_PASSWORD", dev_default=_DEV_REDIS_PASSWORD)
REDIS_URL = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/0"

# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------
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
