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
    """Stable, obviously-non-prod fallback. Computed via hash so no
    password-shaped literal lives in source. Same input across all
    services produces the same value so cross-service JWTs still verify.
    Mirrors services/registry/app/config.py.
    """
    digest = hashlib.sha256(f"agentnet-dev/{name}".encode("utf-8")).hexdigest()
    return "dev-only-" + digest[:48]


_DEV_JWT_SECRET = _dev_only_default("jwt-secret")
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
    """See ``services/registry/app/config.py:require_env`` for the full doc.

    Dev: return the env value as-is if set (so Postgres / Redis credentials
    that match what those containers booted with come straight through),
    else dev_default. Non-dev: reject empty AND reject leftover placeholders.
    """
    val = os.getenv(name, "")
    if IS_DEV:
        if val:
            return val
        if dev_default is not None:
            return dev_default
        raise RuntimeError(f"Required environment variable {name!r} is not set.")

    if not val:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            f"This must be set in non-development environments."
        )
    if val in _LEFTOVER_DEFAULTS:
        raise RuntimeError(
            f"Environment variable {name!r} is set to a known placeholder "
            f"value ({val!r}). Refusing to start in {ENVIRONMENT!r}."
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


# ---------------------------------------------------------------------------
# Internal service-to-service calls (worker → payment)
# ---------------------------------------------------------------------------
# Shared secret presented as `X-Internal-Token` by trusted background workers
# on internal endpoints such as POST /v1/approval_requests/worker/expire.
# Non-development environments must set it explicitly; development uses a
# derived, obviously-non-prod sentinel like the other dev defaults.
_DEV_INTERNAL_WORKER_TOKEN = _dev_only_default("internal-worker-token")
INTERNAL_WORKER_TOKEN = require_env("INTERNAL_WORKER_TOKEN", dev_default=_DEV_INTERNAL_WORKER_TOKEN)
