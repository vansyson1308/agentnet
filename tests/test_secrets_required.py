"""
Regression tests for fail-fast secret validation.

INVARIANT: in any non-development environment, services must refuse to
start if JWT_SECRET_KEY / REDIS_PASSWORD / POSTGRES_PASSWORD are unset
OR set to one of the well-known placeholder strings that ship with the
repo (e.g. ``your_jwt_secret_key``). The placeholders are public and
would let anyone forge tokens or pop the cluster.
"""

import importlib
import os
import sys

import pytest


def _reload_config(module_path: str):
    """Force-reload the given service config module after env mutation."""
    if module_path in sys.modules:
        del sys.modules[module_path]
    return importlib.import_module(module_path)


@pytest.fixture
def clean_env(monkeypatch):
    """Strip all secret-related env vars so each test starts from scratch."""
    for key in (
        "ENVIRONMENT",
        "JWT_SECRET_KEY",
        "REDIS_PASSWORD",
        "POSTGRES_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch


CONFIG_MODULES = [
    "services.registry.app.config",
    "services.payment.app.config",
    "services.worker.app.config",
]


@pytest.mark.parametrize("module_path", CONFIG_MODULES)
class TestRequireEnv:
    def test_dev_default_used_when_unset(self, clean_env, module_path):
        """In ENVIRONMENT=development, missing secret falls back to dev default."""
        clean_env.setenv("ENVIRONMENT", "development")
        cfg = _reload_config(module_path)
        # Each service that owns a JWT secret must have one set.
        if hasattr(cfg, "JWT_SECRET_KEY"):
            assert cfg.JWT_SECRET_KEY  # non-empty
            assert "your_jwt_secret_key" not in cfg.JWT_SECRET_KEY
        assert "your_redis_password" not in cfg.REDIS_PASSWORD

    def test_production_raises_when_secret_missing(self, clean_env, module_path):
        """In production with no secrets set, importing config must raise."""
        clean_env.setenv("ENVIRONMENT", "production")
        # Strip any cached module so the import re-runs require_env.
        sys.modules.pop(module_path, None)
        with pytest.raises(RuntimeError, match="Required environment variable"):
            importlib.import_module(module_path)

    def test_production_rejects_known_placeholder(self, clean_env, module_path):
        """Even if env is set, leftover placeholders from .env.example must be rejected."""
        clean_env.setenv("ENVIRONMENT", "production")
        clean_env.setenv("JWT_SECRET_KEY", "your_jwt_secret_key")
        clean_env.setenv("REDIS_PASSWORD", "your_redis_password")
        clean_env.setenv("POSTGRES_PASSWORD", "your_secure_password")
        sys.modules.pop(module_path, None)
        with pytest.raises(RuntimeError, match="placeholder|not set"):
            importlib.import_module(module_path)

    def test_production_accepts_real_secret(self, clean_env, module_path):
        """Real secrets in production are accepted."""
        clean_env.setenv("ENVIRONMENT", "production")
        real_secret = "f" * 64  # 64 hex chars, simulating openssl rand -hex 32
        clean_env.setenv("JWT_SECRET_KEY", real_secret)
        clean_env.setenv("REDIS_PASSWORD", "real-redis-password-abc123")
        clean_env.setenv("POSTGRES_PASSWORD", "real-postgres-password-xyz789")
        cfg = _reload_config(module_path)
        if hasattr(cfg, "JWT_SECRET_KEY"):
            assert cfg.JWT_SECRET_KEY == real_secret
        assert cfg.REDIS_PASSWORD == "real-redis-password-abc123"

    def test_dev_mode_passes_through_leftover_defaults(self, clean_env, module_path):
        """In development, the value from compose/.env passes through as-is.

        This guarantees the password the app uses to connect to Postgres
        matches the password Postgres was initialised with — even when
        that value is the well-known ``your_secure_password`` placeholder
        that ships in .env.example. require_env only treats those as a
        hard error in non-dev.
        """
        clean_env.setenv("ENVIRONMENT", "development")
        clean_env.setenv("POSTGRES_PASSWORD", "your_secure_password")
        clean_env.setenv("REDIS_PASSWORD", "your_redis_password")
        clean_env.setenv("JWT_SECRET_KEY", "your_jwt_secret_key")
        cfg = _reload_config(module_path)
        assert cfg.POSTGRES_PASSWORD == "your_secure_password"
        assert cfg.REDIS_PASSWORD == "your_redis_password"
        if hasattr(cfg, "JWT_SECRET_KEY"):
            assert cfg.JWT_SECRET_KEY == "your_jwt_secret_key"
