"""Authorization matrix smoke (Phase 2.5 §9): EVERY mutating route on the
registry and payment APIs must reject an anonymous caller with 401/403/404
before doing anything. A route may only be anonymous when it is on the
explicit PUBLIC list below (each entry says why).

This is the cheap, exhaustive complement to the targeted BOLA/IDOR tests in
test_authz_registry.py / test_authz_payment.py: a newly added mutating
route that forgets its auth dependency fails here immediately.
"""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# path -> reason it is legitimately reachable without credentials
PUBLIC_REGISTRY = {
    "/v1/auth/user/register": "account creation",
    "/v1/auth/user/login": "user login",
    "/v1/auth/agent/login": "agent signature login (Ed25519 challenge, replay-protected)",
    "/v1/auth/resend-verification": "verification resend",
    "/v1/agents/public-register": "anonymous agent self-registration (404 unless PUBLIC_AGENT_REGISTRATION_ENABLED)",
    "/v1/orchestrator/oauth/authorize": "partner OAuth (needs client_secret; 404 while ORCHESTRATOR_ENABLED=false)",
    "/v1/orchestrator/oauth/token": "partner OAuth (single-use code; 404 while disabled)",
    "/v1/orchestrator/provision": "partner provisioning (needs client_secret; 404 while disabled)",
    "/v1/society/events": "world-event ingress (needs the ingress token; 401/403 without it)",
}
PUBLIC_PAYMENT: dict[str, str] = {}


def _walk(routes):
    """Recursive walk: FastAPI >= 0.13x keeps included routers as
    ``_IncludedRouter`` objects instead of flattening them into app.routes."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        inner = getattr(r, "routes", None) or getattr(getattr(r, "router", None), "routes", None)
        if inner:
            yield from _walk(inner)


def _mutating_routes(app):
    seen = set()
    for r in _walk(app.routes):
        for m in sorted(r.methods & MUTATING):
            seen.add((m, r.path))
    # union with the OpenAPI document so a schema-visible route can never be missed
    for path, item in app.openapi()["paths"].items():
        for method in item:
            if method.upper() in MUTATING:
                seen.add((method.upper(), path))
    assert len(seen) > 5, f"route discovery looks broken: {sorted(seen)}"
    yield from sorted(seen)


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000000", path)


def _check(client, app, public, label):
    offenders = []
    for method, path in _mutating_routes(app):
        if path in public:
            continue
        for body in ({}, None):
            resp = client.request(method, _fill(path), json=body)
            if resp.status_code not in (401, 403, 404):
                offenders.append(f"{label} {method} {path} -> {resp.status_code} (anonymous, body={body!r})")
                break
    assert not offenders, "mutating routes reachable without credentials:\n" + "\n".join(offenders)


def test_every_registry_mutating_route_rejects_anonymous(api_client):
    from services.registry.app.main import app

    _check(api_client, app, PUBLIC_REGISTRY, "registry")


def test_every_payment_mutating_route_rejects_anonymous(payment_client):
    from services.payment.app.main import app

    _check(payment_client, app, PUBLIC_PAYMENT, "payment")


def test_public_list_matches_reality(api_client):
    """Entries on the PUBLIC list must really exist (no stale allow-listing)."""
    from services.registry.app.main import app

    paths = {p for _, p in _mutating_routes(app)}
    stale = [p for p in PUBLIC_REGISTRY if p not in paths]
    assert not stale, f"PUBLIC_REGISTRY lists routes that no longer exist: {stale}"


@pytest.mark.parametrize("path", sorted(PUBLIC_REGISTRY))
def test_public_routes_still_validate_or_gate(api_client, path):
    """Public does not mean open: an empty anonymous POST must never succeed."""
    resp = api_client.post(path, json={})
    assert resp.status_code in (400, 401, 403, 404, 422), (path, resp.status_code, resp.text[:200])
