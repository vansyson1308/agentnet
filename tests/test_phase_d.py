"""
Regression tests for the Phase D pre-deploy bug fixes.

Each test pins ONE invariant introduced in commit Phase D so a future
refactor cannot silently re-introduce a bug we already paid for.

D1 — WS UUID guard (in source — websocket_manager.py)
D2 — alembic env.py uses pg_advisory_lock
D3 — worker raises (not warns) on prom port collision
D4 — Dockerfiles include --proxy-headers
D5 — rate limiter has _redis_init_lock
D6 — task_service.create_task_with_escrow rejects mismatched payload reuse
D7 — WS handler uses SessionLocal, not the dependency-injected db
"""

from __future__ import annotations

import pathlib
import re
import inspect

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text()


# ─── D1 ─────────────────────────────────────────────────────


def test_d1_ws_uuid_guard_present():
    text = _read("services/registry/app/websocket_manager.py")
    # Either pattern works — what matters is uuid parsing is wrapped.
    assert "except (ValueError, TypeError)" in text
    assert "Invalid agent_id format" in text


# ─── D2 ─────────────────────────────────────────────────────


def test_d2_alembic_advisory_lock_present():
    text = _read("services/registry/migrations/env.py")
    assert "pg_advisory_lock" in text
    assert "pg_advisory_unlock" in text
    # Lock id must be stable.
    assert re.search(r"_ALEMBIC_LOCK_ID\s*=\s*\d+", text)


# ─── D3 ─────────────────────────────────────────────────────


def test_d3_worker_raises_on_metrics_port_collision():
    text = _read("services/worker/app/worker.py")
    # The OSError handler must `raise` — not `pass` and not just log a warning.
    body = re.search(
        r"start_http_server\(WORKER_METRICS_PORT\)(.*?)\n    # ",
        text,
        re.DOTALL,
    )
    assert body is not None, "worker main()'s start_http_server block missing"
    assert "raise" in body.group(0)
    assert "logger.warning" not in body.group(0)


# ─── D4 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "dockerfile",
    [
        "services/registry/Dockerfile",
        "services/payment/Dockerfile",
        "services/simulation/Dockerfile",
    ],
)
def test_d4_dockerfile_uses_proxy_headers(dockerfile):
    text = _read(dockerfile)
    assert "--proxy-headers" in text
    assert "--forwarded-allow-ips" in text


def test_d4_agent_card_uses_forwarded_headers():
    text = _read("services/registry/app/main.py")
    assert "x-forwarded-host" in text.lower()
    assert "x-forwarded-proto" in text.lower()


# ─── D5 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "limiter_path",
    [
        "services/registry/app/api/rate_limiter.py",
        "services/payment/app/api/rate_limiter.py",
    ],
)
def test_d5_rate_limiter_has_init_lock(limiter_path):
    text = _read(limiter_path)
    assert "_redis_init_lock" in text
    assert "asyncio.Lock" in text or "asyncio_Lock" in text
    assert "asyncio.wait_for" in text  # ensures init has timeout


# ─── D6 ─────────────────────────────────────────────────────


def test_d6_idempotency_payload_hash_check():
    text = _read("services/registry/app/task_service.py")
    assert "request_hash" in text
    assert "Idempotency-Key reused with a different request payload" in text


# ─── D7 ─────────────────────────────────────────────────────


def test_d7_ws_uses_sessionlocal_per_message():
    text = _read("services/registry/app/websocket_manager.py")
    # Must import SessionLocal inside _handle_task_method and use it
    # for the message-scoped session.
    assert "from .database import SessionLocal" in text
    assert "owned_db = SessionLocal()" in text
    assert "owned_db.close()" in text


# ─── E5 / E6 ────────────────────────────────────────────────


def test_e5_balance_check_migration_exists():
    p = REPO / "services/registry/migrations/versions/0004_balance_checks.py"
    assert p.exists()
    text = p.read_text()
    assert "chk_balance_credits_nonneg" in text
    assert "chk_reserved_credits_nonneg" in text


def test_e6_platform_fee_min_callee_share():
    p = REPO / "services/registry/migrations/versions/0005_platform_fee_min_callee.py"
    assert p.exists()
    text = p.read_text()
    # The new formula caps the fee so net_amount >= 1.
    assert "LEAST(" in text
    assert "NEW.amount - 1" in text


# ─── E1 (healthchecks) ──────────────────────────────────────


@pytest.mark.parametrize(
    "compose",
    [
        "docker-compose.prod.yml",
        "docker-compose.staging.yml",
    ],
)
def test_e1_healthchecks_present(compose):
    text = _read(compose)
    # At least one healthcheck per service that exposes HTTP.
    healthcheck_count = text.count("healthcheck:")
    assert healthcheck_count >= 3, f"{compose} should have ≥3 healthchecks, found {healthcheck_count}"


# ─── E3 (werewolf removal) ──────────────────────────────────


def test_e3_no_werewolf_templates():
    templates = list((REPO / "services/dashboard/app/templates").glob("werewolf_*.html"))
    assert templates == []


def test_e3_no_werewolf_static():
    css = REPO / "services/dashboard/app/static/css/werewolf.css"
    js = REPO / "services/dashboard/app/static/js/werewolf.js"
    assert not css.exists()
    assert not js.exists()


def test_e3_compose_no_werewolf_env():
    for c in ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.staging.yml"):
        text = _read(c)
        assert "WEREWOLF_STATE_FILE" not in text, f"{c} still references werewolf"
        assert "werewolf_data:/app/werewolf_data" not in text


# ─── E2 (Caddyfile subdomains) ──────────────────────────────


def test_e2_caddyfile_subdomain_blocks():
    text = _read("deploy/Caddyfile")
    assert "agentnet.io.vn" in text
    assert "payment.agentnet.io.vn" in text
    assert "staging.agentnet.io.vn" in text
    assert "dashboard.agentnet.io.vn" in text


# ─── E4 (dashboard hardening) ───────────────────────────────


def test_e4_dashboard_requires_flask_secret_in_prod():
    text = _read("services/dashboard/app/main.py")
    assert "FLASK_SECRET_KEY is required in non-development" in text
    assert "ProxyFix" in text
    assert "SESSION_COOKIE_HTTPONLY" in text
