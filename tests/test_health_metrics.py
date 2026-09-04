"""
Smoke tests for /healthz, /readyz, /metrics endpoints across services.

Verifies:
- Each FastAPI service exposes the three endpoints with expected shape.
- /metrics responds in Prometheus text format.
- The shared metrics middleware bumps http_requests_total per request.

These don't need a real DB — readyz is exercised against a stubbed engine
that raises, and we assert the 503 path. The "DB+Redis up" case is covered
by integration tests (live stack required).
"""

from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest


def _install_module_stubs(monkeypatch):
    """Provide stubs for ed25519 + the registry's optional modules so we
    can import services.registry.app.health without booting the full app."""
    # ed25519 isn't installable in this CI env — fake it.
    if "ed25519" not in sys.modules:
        fake = MagicMock()
        sys.modules["ed25519"] = fake
    monkeypatch.setenv("ENVIRONMENT", "development")


@pytest.fixture
def registry_app(monkeypatch):
    _install_module_stubs(monkeypatch)
    # Force a fresh import so the env patch takes effect.
    for k in list(sys.modules):
        if k.startswith("services.registry.app.health") or k.startswith(
            "services.registry.app.config"
        ):
            sys.modules.pop(k, None)
    health = importlib.import_module("services.registry.app.health")
    from fastapi import FastAPI

    app = FastAPI()
    health.install_health_and_metrics(app, service_name="registry-test")
    return app, health


@pytest.fixture
def payment_app(monkeypatch):
    _install_module_stubs(monkeypatch)
    for k in list(sys.modules):
        if k.startswith("services.payment.app.health") or k.startswith(
            "services.payment.app.config"
        ):
            sys.modules.pop(k, None)
    health = importlib.import_module("services.payment.app.health")
    from fastapi import FastAPI

    app = FastAPI()
    health.install_health_and_metrics(app, service_name="payment-test")
    return app, health


class TestHealthz:
    def test_registry_healthz_200(self, registry_app):
        from fastapi.testclient import TestClient

        app, _ = registry_app
        client = TestClient(app)
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["service"] == "registry-test"

    def test_payment_healthz_200(self, payment_app):
        from fastapi.testclient import TestClient

        app, _ = payment_app
        client = TestClient(app)
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["service"] == "payment-test"


class TestReadyz:
    def test_readyz_503_when_db_down(self, registry_app, monkeypatch):
        from fastapi.testclient import TestClient

        app, health = registry_app

        # Stub engine.connect to raise.
        class _BoomEngine:
            def connect(self):
                raise RuntimeError("db down")

        monkeypatch.setattr(health, "engine", _BoomEngine())

        client = TestClient(app)
        resp = client.get("/readyz")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "not_ready" and "db" in body["failing"]
        # component names only — never the exception text (DSN, host, user)
        assert "db down" not in resp.text

    def test_readyz_503_when_redis_down_without_leaking_details(self, registry_app, monkeypatch):
        from fastapi.testclient import TestClient

        app, health = registry_app

        class _OkConn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, *a, **k):
                return None

        class _OkEngine:
            def connect(self):
                return _OkConn()

        monkeypatch.setattr(health, "engine", _OkEngine())
        monkeypatch.setattr(health, "REDIS_URL", "redis://:hunter2@redis.internal.invalid:6379/0")

        resp = TestClient(app).get("/readyz")
        assert resp.status_code == 503
        assert resp.json() == {"status": "not_ready", "failing": ["redis"]}
        assert "hunter2" not in resp.text and "redis.internal" not in resp.text

    def test_readyz_200_when_both_dependencies_answer(self, registry_app, monkeypatch):
        from fastapi.testclient import TestClient

        app, health = registry_app

        class _OkConn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, *a, **k):
                return None

        class _OkEngine:
            def connect(self):
                return _OkConn()

        class _OkRedis:
            async def ping(self):
                return True

            async def aclose(self):
                return None

        monkeypatch.setattr(health, "engine", _OkEngine())
        monkeypatch.setattr(health.redis, "from_url", lambda *a, **k: _OkRedis())
        resp = TestClient(app).get("/readyz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"


class TestMetrics:
    def test_metrics_returns_prometheus_format(self, registry_app):
        from fastapi.testclient import TestClient

        app, _ = registry_app
        client = TestClient(app)
        # Hit a non-existent route first so the middleware records something.
        client.get("/healthz")
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "agentnet_http_requests_total" in resp.text
        # Should mention our service label.
        assert 'service="registry-test"' in resp.text

    def test_metrics_middleware_skips_metrics_endpoint(self, registry_app):
        """Hitting /metrics itself must NOT increment the counter for /metrics."""
        from fastapi.testclient import TestClient

        app, _ = registry_app
        client = TestClient(app)
        client.get("/metrics")
        resp = client.get("/metrics").text
        # No path="/metrics" label should appear (middleware short-circuits).
        assert 'path="/metrics"' not in resp


class TestWiring:
    """The main.py of each service must invoke install_health_and_metrics."""

    @pytest.mark.parametrize(
        "main_path",
        [
            "services/registry/app/main.py",
            "services/payment/app/main.py",
            "services/simulation/app/main.py",
        ],
    )
    def test_main_calls_install_health_and_metrics(self, main_path):
        import pathlib

        text = (pathlib.Path(__file__).parent.parent / main_path).read_text()
        assert "install_health_and_metrics(app" in text, (
            f"{main_path} must call install_health_and_metrics()"
        )

    def test_worker_starts_prometheus_http_server(self):
        import pathlib

        text = (
            pathlib.Path(__file__).parent.parent / "services/worker/app/worker.py"
        ).read_text()
        assert "start_http_server(WORKER_METRICS_PORT)" in text


class TestOptionalSurfacesDefaultOff:
    """Phase 2.5 §6/§9: privileged or anonymous surfaces are opt-in."""

    def test_flags_default_off(self, monkeypatch):
        import importlib
        import sys

        for k in ("AUTO_SCALER_ENABLED", "PUBLIC_AGENT_REGISTRATION_ENABLED", "ORCHESTRATOR_ENABLED"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("ENVIRONMENT", "development")
        sys.modules.pop("services.registry.app.config", None)
        cfg = importlib.import_module("services.registry.app.config")
        assert cfg.AUTO_SCALER_ENABLED is False
        assert cfg.PUBLIC_AGENT_REGISTRATION_ENABLED is False
        assert cfg.ORCHESTRATOR_ENABLED is False

    def test_registry_startup_skips_auto_scaler_when_disabled(self, monkeypatch):
        """The startup hook must not launch the Docker-socket scaler unless enabled."""
        import pathlib

        text = pathlib.Path("services/registry/app/main.py").read_text(encoding="utf-8")
        assert "if AUTO_SCALER_ENABLED:" in text
        assert text.index("if AUTO_SCALER_ENABLED:") < text.index("await start_auto_scaler()")
