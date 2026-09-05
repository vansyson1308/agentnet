"""
FastAPI lifespan regression (Phase 2.6 §6.4).

The three FastAPI applications used to register ``@app.on_event("startup")``
/ ``("shutdown")`` handlers (deprecated since FastAPI 0.93; a lifespan and
event handlers cannot be mixed). They now use ONE lifespan context each.
These tests enter and exit that lifespan through Starlette's TestClient and
prove:

* no event handler is registered any more (lifespan is the only mechanism);
* startup runs exactly once per application lifetime and in the original
  order (registry: Redis pub/sub for the WebSocket manager, then the opt-in
  auto-scaler);
* shutdown runs exactly once, flushes the tracer provider FIRST and stops the
  auto-scaler AFTER it — and no longer raises: ``TracerProvider.shutdown()``
  is synchronous, the old handlers awaited its ``None`` return value;
* the auto-scaler stays off unless ``AUTO_SCALER_ENABLED`` is set.

Redis and the tracer are replaced by fakes so the tests are hermetic.
"""

from __future__ import annotations

import importlib
import inspect
import os
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("JAEGER_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
MAINS = {
    "registry": "services.registry.app.main",
    "payment": "services.payment.app.main",
    "simulation": "services.simulation.app.main",
}


@pytest.mark.parametrize("svc", sorted(MAINS))
def test_only_lifespan_no_event_handlers(svc):
    mod = importlib.import_module(MAINS[svc])
    app = mod.app
    assert app.router.on_startup == [], "startup event handlers must not coexist with the lifespan"
    assert app.router.on_shutdown == [], "shutdown event handlers must not coexist with the lifespan"
    assert app.router.lifespan_context is not None
    src = (REPO / "services" / svc / "app" / "main.py").read_text(encoding="utf-8")
    assert "@app.on_event(" not in src
    assert "lifespan=lifespan" in src


def test_tracer_provider_shutdown_is_synchronous():
    """The old handlers did ``await tracer_provider.shutdown()`` — a TypeError
    at every shutdown. Pin the contract the lifespans now rely on."""
    from opentelemetry.sdk.trace import TracerProvider

    assert not inspect.iscoroutinefunction(TracerProvider.shutdown)


def _fake_tracer(calls, label="tracer_shutdown"):
    return MagicMock(shutdown=MagicMock(side_effect=lambda: calls.append(label)))


def test_registry_lifespan_order_with_auto_scaler_disabled(monkeypatch):
    mod = importlib.import_module(MAINS["registry"])
    calls: list[str] = []
    monkeypatch.setattr(mod.manager, "init_redis", AsyncMock(side_effect=lambda: calls.append("init_redis")))
    monkeypatch.setattr(mod, "tracer_provider", _fake_tracer(calls))
    monkeypatch.setattr(mod, "start_auto_scaler", AsyncMock(side_effect=AssertionError("auto-scaler must stay off")))
    stop = AsyncMock(side_effect=lambda: calls.append("stop_auto_scaler"))
    monkeypatch.setattr(mod, "stop_auto_scaler", stop)
    monkeypatch.setattr(importlib.import_module("services.registry.app.config"), "AUTO_SCALER_ENABLED", False)

    with TestClient(mod.app) as client:
        assert calls == ["init_redis"]
        assert mod._auto_scaler_task is None
        for _ in range(3):
            assert client.get("/health").status_code == 200
        assert calls == ["init_redis"], "startup must run exactly once per application lifetime"
    assert calls == ["init_redis", "tracer_shutdown"], "shutdown flushes the tracer; nothing to stop when the scaler is off"
    stop.assert_not_awaited()


def test_registry_lifespan_order_with_auto_scaler_enabled(monkeypatch):
    mod = importlib.import_module(MAINS["registry"])
    calls: list[str] = []
    sentinel = object()
    monkeypatch.setattr(mod.manager, "init_redis", AsyncMock(side_effect=lambda: calls.append("init_redis")))
    monkeypatch.setattr(mod, "tracer_provider", _fake_tracer(calls))

    async def _start():
        calls.append("start_auto_scaler")
        return sentinel

    async def _stop():
        calls.append("stop_auto_scaler")

    monkeypatch.setattr(mod, "start_auto_scaler", _start)
    monkeypatch.setattr(mod, "stop_auto_scaler", _stop)
    monkeypatch.setattr(importlib.import_module("services.registry.app.config"), "AUTO_SCALER_ENABLED", True)

    with TestClient(mod.app):
        assert calls == ["init_redis", "start_auto_scaler"], "Redis first, then the scaler (original order)"
        assert mod._auto_scaler_task is sentinel
    assert calls == ["init_redis", "start_auto_scaler", "tracer_shutdown", "stop_auto_scaler"]
    assert mod._auto_scaler_task is None, "shutdown must release the task reference"


def test_registry_lifespan_runs_cleanup_even_when_the_app_body_fails(monkeypatch):
    """try/finally around ``yield``: cleanup happens even if the lifespan is
    torn down abnormally (the shutdown side must never be skipped)."""
    mod = importlib.import_module(MAINS["registry"])
    calls: list[str] = []
    monkeypatch.setattr(mod.manager, "init_redis", AsyncMock())
    monkeypatch.setattr(mod, "tracer_provider", _fake_tracer(calls))
    monkeypatch.setattr(importlib.import_module("services.registry.app.config"), "AUTO_SCALER_ENABLED", False)

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with TestClient(mod.app):
            raise Boom()
    assert calls == ["tracer_shutdown"]


@pytest.mark.parametrize("svc", ["payment", "simulation"])
def test_stateless_services_flush_tracer_once_on_shutdown(svc, monkeypatch):
    mod = importlib.import_module(MAINS[svc])
    calls: list[str] = []
    monkeypatch.setattr(mod, "tracer_provider", _fake_tracer(calls))
    with TestClient(mod.app) as client:
        assert calls == []
        assert client.get("/health").status_code == 200
        assert client.get("/health").status_code == 200
    assert calls == ["tracer_shutdown"]
    # A second lifetime shuts down again exactly once — no double init, no skipped cleanup.
    with TestClient(mod.app):
        pass
    assert calls == ["tracer_shutdown", "tracer_shutdown"]
