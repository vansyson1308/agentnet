"""
Simulation service configuration.

Loads LLM, Zep, and simulation settings from environment variables.
Also exposes JWT/Redis/Postgres secrets via fail-fast `require_env` so the
service refuses to start in non-development environments without real
secrets.
"""

import hashlib
import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Environment + secret validation (mirrors registry/payment config.py)
# ---------------------------------------------------------------------------
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
IS_DEV = ENVIRONMENT == "development"


def _dev_only_default(name: str) -> str:
    """Stable, hash-derived dev fallback (see registry/app/config.py)."""
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
            f"Required environment variable {name!r} is not set. "
            f"This must be set in non-development environments."
        )
    if val in _LEFTOVER_DEFAULTS:
        raise RuntimeError(
            f"Environment variable {name!r} is set to a known placeholder "
            f"value ({val!r}). Refusing to start in {ENVIRONMENT!r}."
        )
    return val


JWT_SECRET_KEY = require_env("JWT_SECRET_KEY", dev_default=_DEV_JWT_SECRET)
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRATION = int(os.getenv("JWT_EXPIRATION", "3600"))

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_PASSWORD = require_env("REDIS_PASSWORD", dev_default=_DEV_REDIS_PASSWORD)
REDIS_URL = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/0"

# Postgres — same shape as registry/payment/worker config.py. The password
# goes through require_env so a non-development deployment cannot start with
# a missing or placeholder password (database.py used to hard-code a fallback).
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_USER = os.getenv("POSTGRES_USER", "agentnet")
POSTGRES_DB = os.getenv("POSTGRES_DB", "agentnet")
POSTGRES_PASSWORD = require_env("POSTGRES_PASSWORD", dev_default=_DEV_POSTGRES_PASSWORD)
DATABASE_URL = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"


class SimulationConfig:
    """Configuration for the simulation service."""

    # LLM API (OpenAI SDK format — works with any compatible provider)
    LLM_API_KEY = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME", "gpt-4")

    # Zep Cloud (knowledge graph memory)
    ZEP_API_KEY = os.getenv("ZEP_API_KEY", "")

    # Simulation defaults
    DEFAULT_MAX_STEPS = int(os.getenv("SIM_DEFAULT_MAX_STEPS", "100"))
    DEFAULT_PLATFORM = os.getenv("SIM_DEFAULT_PLATFORM", "twitter")
    SIMULATION_TIMEOUT_SECONDS = int(os.getenv("SIMULATION_TIMEOUT_SECONDS", "600"))

    # Cost per simulation step (in credits)
    COST_PER_STEP = int(os.getenv("SIM_COST_PER_STEP", "5"))
    COST_BASE = int(os.getenv("SIM_COST_BASE", "50"))

    # Report Agent
    REPORT_MAX_TOOL_CALLS = int(os.getenv("REPORT_AGENT_MAX_TOOL_CALLS", "5"))
    REPORT_MAX_REFLECTION_ROUNDS = int(os.getenv("REPORT_AGENT_MAX_REFLECTION_ROUNDS", "2"))
    REPORT_TEMPERATURE = float(os.getenv("REPORT_AGENT_TEMPERATURE", "0.5"))

    # Registry / Payment service URLs (for escrow integration)
    REGISTRY_URL = os.getenv("REGISTRY_URL", "http://registry:8000")
    PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment:8001")

    @classmethod
    def is_llm_configured(cls) -> bool:
        """Check if LLM API is configured."""
        return bool(cls.LLM_API_KEY)

    @classmethod
    def is_zep_configured(cls) -> bool:
        """Check if Zep is configured."""
        return bool(cls.ZEP_API_KEY)

    @classmethod
    def validate(cls) -> list:
        """Validate required configuration. Returns list of errors."""
        errors = []
        if not cls.LLM_API_KEY:
            errors.append("LLM_API_KEY not configured")
        if not cls.ZEP_API_KEY:
            errors.append("ZEP_API_KEY not configured (knowledge graph disabled)")
        return errors
