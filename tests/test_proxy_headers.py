"""Managed-platform edge client address (Phase 4, ADR-0006 D5).

Why the registry does not use ``FORWARDED_ALLOW_IPS=*`` on Railway: uvicorn's
``ProxyHeadersMiddleware`` with ``*`` trusts every peer and returns the
LEFTMOST ``X-Forwarded-For`` entry — the one the client wrote — so the
per-IP rate-limit key of anonymous endpoints (login / register) would be
spoofable. Railway documents ``X-Real-IP`` (not ``X-Forwarded-For``) as the
client address, so the registry trusts exactly that header, only when
``TRUST_X_REAL_IP=true``, and never reads ``X-Forwarded-For``.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
from unittest.mock import patch

import pytest
from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from services.registry.app.proxy_headers import EdgeClientAddressMiddleware, trust_x_real_ip_enabled

REPO = pathlib.Path(__file__).resolve().parent.parent
MAIN = REPO / "services/registry/app/main.py"


def _run(middleware, headers, scope_type="http", client=("10.0.0.5", 4321), scheme="http"):
    seen = {}

    async def inner(scope, receive, send):
        seen.update(scope)

    original = {"type": scope_type, "headers": headers, "client": client, "scheme": scheme, "path": "/"}
    asyncio.run(middleware(inner)(dict(original), None, None))
    return seen, original


def test_x_real_ip_sets_the_client_and_forwarded_proto_sets_the_scheme():
    seen, _ = _run(EdgeClientAddressMiddleware, [(b"x-real-ip", b"203.0.113.9"), (b"x-forwarded-proto", b"https")])
    assert seen["client"] == ("203.0.113.9", 4321)
    assert seen["scheme"] == "https"
    # the rate limiter keys anonymous callers by request.client.host
    assert Request(seen).client.host == "203.0.113.9"


def test_x_forwarded_for_is_never_read():
    seen, _ = _run(EdgeClientAddressMiddleware, [(b"x-forwarded-for", b"203.0.113.9, 10.1.1.1")])
    assert seen["client"] == ("10.0.0.5", 4321)
    assert seen["scheme"] == "http"


@pytest.mark.parametrize("bad", [b"not-an-ip", b"203.0.113.9, 10.1.1.1", b"", b"  ", b"203.0.113.9:443"])
def test_non_ip_x_real_ip_values_are_ignored(bad):
    seen, _ = _run(EdgeClientAddressMiddleware, [(b"x-real-ip", bad)])
    assert seen["client"] == ("10.0.0.5", 4321)


def test_last_x_real_ip_header_wins_and_ipv6_is_accepted():
    seen, _ = _run(EdgeClientAddressMiddleware, [(b"x-real-ip", b"198.51.100.7"), (b"x-real-ip", b" 2001:db8::9 ")])
    assert seen["client"] == ("2001:db8::9", 4321)


@pytest.mark.parametrize("proto, expected", [(b"https", "wss"), (b"http", "ws"), (b"https, http", "wss")])
def test_websocket_scheme_mapping(proto, expected):
    seen, _ = _run(EdgeClientAddressMiddleware, [(b"x-forwarded-proto", proto)], scope_type="websocket", scheme="ws")
    assert seen["scheme"] == expected


def test_unknown_proto_and_missing_headers_leave_the_scope_alone():
    seen, original = _run(EdgeClientAddressMiddleware, [(b"x-forwarded-proto", b"gopher")])
    assert seen["client"] == original["client"] and seen["scheme"] == original["scheme"]
    seen, original = _run(EdgeClientAddressMiddleware, [])
    assert seen["client"] == original["client"] and seen["scheme"] == original["scheme"]


def test_the_callers_scope_object_is_not_mutated():
    seen = {}

    async def inner(scope, receive, send):
        seen.update(scope)

    scope = {"type": "http", "headers": [(b"x-real-ip", b"203.0.113.9")], "client": ("10.0.0.5", 1), "scheme": "http"}
    asyncio.run(EdgeClientAddressMiddleware(inner)(scope, None, None))
    assert scope["client"] == ("10.0.0.5", 1) and seen["client"] == ("203.0.113.9", 1)


def test_lifespan_scope_passes_through():
    seen, _ = _run(EdgeClientAddressMiddleware, [(b"x-real-ip", b"203.0.113.9")], scope_type="lifespan", client=None)
    assert seen["client"] is None


@pytest.mark.parametrize("value, expected", [("true", True), ("True ", True), ("false", False), ("1", False), ("", False)])
def test_trust_flag_parsing(value, expected):
    with patch.dict("os.environ", {"TRUST_X_REAL_IP": value}):
        assert trust_x_real_ip_enabled() is expected
    with patch.dict("os.environ", {}, clear=True):
        assert trust_x_real_ip_enabled() is False


def test_uvicorn_star_would_honour_the_client_controlled_leftmost_forwarded_for():
    """The reason FORWARDED_ALLOW_IPS=* is refused on Railway (pinned uvicorn)."""
    seen, _ = _run(
        lambda app: ProxyHeadersMiddleware(app, trusted_hosts="*"),
        [(b"x-forwarded-for", b"203.0.113.9, 10.1.1.1"), (b"x-forwarded-proto", b"https")],
    )
    assert seen["client"][0] == "203.0.113.9", "with '*' uvicorn takes the leftmost (client-written) entry"
    # and with the default trust list the same header is ignored from a non-loopback peer
    seen, _ = _run(lambda app: ProxyHeadersMiddleware(app), [(b"x-forwarded-for", b"203.0.113.9")])
    assert seen["client"][0] == "10.0.0.5"


def test_registry_enables_the_middleware_only_by_flag_and_outside_the_rate_limiter():
    text = MAIN.read_text(encoding="utf-8")
    assert "if trust_x_real_ip_enabled():\n    app.add_middleware(EdgeClientAddressMiddleware)" in text
    assert text.index("EdgeClientAddressMiddleware)") > text.index("RateLimitMiddleware,"), "added after → wraps the rate limiter"
    assert re.search(r"FORWARDED_ALLOW_IPS[\"']?\s*[=\]]|forwarded_allow_ips\s*=", text) is None, (
        "uvicorn's own trust list is configured per deployment, never set in code"
    )
