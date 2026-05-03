"""
Tests for structured logging + request-id middleware.

Verify:
- setup_logging emits JSON when ENVIRONMENT != development.
- Records carry the bound `service` field.
- RequestIDMiddleware injects a fresh uuid OR honours inbound X-Request-ID
  and propagates it on the response.
- No `print(...)` calls remain in service code.
"""

from __future__ import annotations

import importlib
import io
import json
import logging
import os
import pathlib
import sys
import uuid

import pytest


@pytest.fixture
def reload_logging_config(monkeypatch):
    def _do(env: str = "production"):
        monkeypatch.setenv("ENVIRONMENT", env)
        # Force re-import so module-level `_IS_DEV` reflects the new env.
        sys.modules.pop("services.registry.app.logging_config", None)
        return importlib.import_module("services.registry.app.logging_config")

    return _do


class TestStructlogJSON:
    def test_prod_emits_json_with_service_field(self, reload_logging_config, capsys):
        cfg = reload_logging_config("production")
        cfg.setup_logging("test-service")
        logging.getLogger("test").info("hello world", extra={"k": "v"})
        captured = capsys.readouterr().out.strip()
        assert captured, "should have emitted at least one line"
        last = captured.splitlines()[-1]
        # Must be parseable as JSON.
        rec = json.loads(last)
        assert rec.get("service") == "test-service"
        assert rec.get("event") == "hello world"
        assert rec.get("level") == "info"

    def test_dev_emits_console_format(self, reload_logging_config, capsys):
        cfg = reload_logging_config("development")
        cfg.setup_logging("dev-service")
        logging.getLogger("test").warning("dev message")
        captured = capsys.readouterr().out
        # Console renderer prints "warning" + "dev message" but not as JSON.
        assert "dev message" in captured
        # Should NOT be a single JSON object on the line.
        last = captured.strip().splitlines()[-1]
        with pytest.raises(json.JSONDecodeError):
            json.loads(last)


class TestRequestIDMiddleware:
    def test_assigns_uuid_when_no_header(self, reload_logging_config):
        cfg = reload_logging_config("development")
        cfg.setup_logging("test")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        cfg.install_request_id_middleware(app)

        @app.get("/")
        async def root():
            return {"ok": True}

        client = TestClient(app)
        resp = client.get("/")
        rid = resp.headers.get("X-Request-ID")
        assert rid is not None
        # Validates as a UUID.
        uuid.UUID(rid)

    def test_propagates_inbound_header(self, reload_logging_config):
        cfg = reload_logging_config("development")
        cfg.setup_logging("test")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        cfg.install_request_id_middleware(app)

        @app.get("/")
        async def root():
            return {"ok": True}

        client = TestClient(app)
        resp = client.get("/", headers={"X-Request-ID": "abc-123"})
        assert resp.headers.get("X-Request-ID") == "abc-123"


class TestNoPrintStatements:
    """Once we've migrated, prod code paths must not print() — every line
    has to flow through the structured logger so log aggregators can
    index it. This regression test scans the service trees."""

    def test_no_print_in_services(self):
        repo = pathlib.Path(__file__).parent.parent
        offenders = []
        for service in ("registry", "payment", "simulation", "worker"):
            for py in (repo / "services" / service / "app").rglob("*.py"):
                text = py.read_text()
                # Allow `print()` only inside string literals or comments
                # by relying on the simple heuristic — no whitespace-only
                # line that opens with `print(`. False positives caught by
                # explicit allowlist below.
                for i, line in enumerate(text.splitlines(), start=1):
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if stripped.startswith("print("):
                        offenders.append(f"{py.relative_to(repo)}:{i}: {stripped}")
        assert not offenders, "Replace print() with logger calls:\n" + "\n".join(offenders)


class TestWiring:
    @pytest.mark.parametrize(
        "main_path",
        [
            "services/registry/app/main.py",
            "services/payment/app/main.py",
            "services/simulation/app/main.py",
        ],
    )
    def test_main_calls_setup_logging(self, main_path):
        repo = pathlib.Path(__file__).parent.parent
        text = (repo / main_path).read_text()
        assert "setup_logging(" in text
        assert "install_request_id_middleware(app)" in text

    def test_worker_calls_setup_logging(self):
        repo = pathlib.Path(__file__).parent.parent
        text = (repo / "services/worker/app/worker.py").read_text()
        assert 'setup_logging("worker")' in text
