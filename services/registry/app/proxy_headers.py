"""Client address behind a managed platform edge (ADR-0006 D5; docs/RAILWAY_STAGING.md §11).

uvicorn's ``--proxy-headers`` rewrites ``scope["client"]`` / ``scope["scheme"]``
from ``X-Forwarded-For`` / ``X-Forwarded-Proto`` — but only for peers listed in
``FORWARDED_ALLOW_IPS`` (default ``127.0.0.1``). Railway publishes no ingress
CIDR and documents ``X-Real-IP`` — not ``X-Forwarded-For`` — as the client's
remote address, so the only way to make uvicorn honour that edge would be
``FORWARDED_ALLOW_IPS=*``. With ``*`` uvicorn trusts every peer and returns the
LEFTMOST ``X-Forwarded-For`` entry, which the client controls: any anonymous
caller could pick a fresh rate-limit bucket per request (login / register
brute force). This middleware is the narrow alternative:

* enabled only when ``TRUST_X_REAL_IP=true`` — a deployment decision made once
  per platform where the container port is reachable exclusively through that
  edge or a private mesh of first-party services;
* the client address comes from the LAST ``X-Real-IP`` header (the edge sets
  it; a value that is not an IP address is ignored) and the scheme from
  ``X-Forwarded-Proto`` (``https`` → ``wss`` for WebSocket scopes);
* ``X-Forwarded-For`` is never read here, and ``FORWARDED_ALLOW_IPS`` keeps its
  default so uvicorn never reads it either.

The post-deploy proof (spoof test) is docs/RAILWAY_STAGING.md §11: if a
client-supplied ``X-Real-IP`` reaches the application unchanged, set
``TRUST_X_REAL_IP=false`` and record the platform finding — never widen trust.
"""

from __future__ import annotations

import ipaddress
import os


def trust_x_real_ip_enabled() -> bool:
    return os.getenv("TRUST_X_REAL_IP", "false").strip().lower() == "true"


def _valid_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


class EdgeClientAddressMiddleware:
    """Pure ASGI middleware: no body access; the scope is shallow-copied only
    when a rewrite happens, so the caller's scope is never mutated."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            real_ip = None
            proto = None
            for name, value in scope.get("headers") or ():
                if name == b"x-real-ip":
                    real_ip = value.decode("latin-1").strip()  # last occurrence wins
                elif name == b"x-forwarded-proto":
                    proto = value.decode("latin-1").split(",")[0].strip().lower()
            client_ip = _valid_ip(real_ip)
            if client_ip or proto in ("http", "https"):
                scope = dict(scope)
                if client_ip:
                    port = scope["client"][1] if scope.get("client") else 0
                    scope["client"] = (client_ip, port)
                if proto in ("http", "https"):
                    if scope["type"] == "websocket":
                        scope["scheme"] = "wss" if proto == "https" else "ws"
                    else:
                        scope["scheme"] = proto
        await self.app(scope, receive, send)
