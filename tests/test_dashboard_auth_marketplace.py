"""Dashboard /login, /register, /logout and /marketplace (restored after
f33f067 deleted them): real pages, forms that post to the registry's
/v1/auth/user/* API through api_client, and a marketplace that renders an
empty state -- never a redirect or a 500 -- when the registry is down."""

from __future__ import annotations

import pytest


@pytest.fixture
def dash():
    from services.dashboard.app import main

    main.app.config.update(TESTING=True)
    return main, main.app.test_client()


def _raise(exc):
    def f(*a, **k):
        raise exc

    return f


def test_marketplace_renders_an_empty_state_when_the_registry_is_unreachable(dash, monkeypatch):
    main, client = dash
    monkeypatch.setattr(main.api_client, "fetch_agents", _raise(main.APIError("connection refused", 500)))
    r = client.get("/marketplace")
    assert r.status_code == 200
    assert b"Agent Registry" in r.data and b"unreachable" in r.data


def test_marketplace_shows_capability_names_not_dicts(dash, monkeypatch):
    main, client = dash
    agent = {"id": "a1", "name": "Echo", "status": "active", "capabilities": [{"name": "echo", "description": "Echo", "price": 1}]}
    monkeypatch.setattr(main.api_client, "fetch_agents", lambda **kw: [agent])
    r = client.get("/marketplace?search=echo")
    assert r.status_code == 200 and b">echo<" in r.data and b"{&#39;name&#39;" not in r.data


def test_metaverse_capability_badges_show_names(dash, monkeypatch):
    main, client = dash
    agent = {"id": "a1", "name": "Echo", "capabilities": [{"name": "summarize", "price": 3}]}
    monkeypatch.setattr(main.api_client, "fetch_agents", lambda **kw: [agent])
    r = client.get("/metaverse")
    assert r.status_code == 200 and b">summarize<" in r.data and b"&#39;price&#39;" not in r.data


def test_login_posts_to_the_registry_and_stores_only_the_token(dash, monkeypatch):
    main, client = dash
    calls = []

    def login(email, password):
        calls.append((email, password))
        return {"access_token": "tok", "token_type": "bearer", "expires_in": 3600}

    monkeypatch.setattr(main.api_client, "login", login)
    r = client.post("/login", data={"email": "a@b.co", "password": "Secret123456"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/metaverse")
    assert calls == [("a@b.co", "Secret123456")]
    with client.session_transaction() as s:
        assert s["access_token"] == "tok"
    r = client.get("/logout")
    assert r.status_code == 302
    with client.session_transaction() as s:
        assert "access_token" not in s


@pytest.mark.parametrize("status,text", [(401, b"Invalid email or password"), (403, b"verify your email"), (503, b"unavailable right now")])
def test_login_failures_render_the_form_with_a_message(dash, monkeypatch, status, text):
    main, client = dash
    monkeypatch.setattr(main.api_client, "login", _raise(main.APIError("http://registry.internal/... failed", status)))
    r = client.post("/login", data={"email": "a@b.co", "password": "x"})
    assert r.status_code in (400, 503) and text in r.data and b'name="password"' in r.data
    assert b"registry.internal" not in r.data


def test_register_posts_to_the_registry_then_sends_the_user_to_login(dash, monkeypatch):
    main, client = dash
    calls = []
    monkeypatch.setattr(main.api_client, "register", lambda email, password: calls.append(email) or {"id": "u1"})
    r = client.post("/register", data={"email": "new@b.co", "password": "Secret123456"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/login") and calls == ["new@b.co"]


def test_register_shows_the_registry_policy_message_only(dash, monkeypatch):
    main, client = dash
    err = main.APIError("400 Client Error for url: http://registry.internal/v1/auth/user/register", 400, "Password must be at least 12 characters long")
    monkeypatch.setattr(main.api_client, "register", _raise(err))
    r = client.post("/register", data={"email": "new@b.co", "password": "short"})
    assert r.status_code == 400 and b"at least 12 characters" in r.data and b"registry.internal" not in r.data


def test_api_client_auth_calls_use_the_registry_user_auth_routes(monkeypatch):
    from services.dashboard.app import api_client as mod

    seen = []
    monkeypatch.setattr(mod.ApiClient, "_request", lambda self, method, path, **kw: seen.append((method, path, kw["json"])) or {})
    c = mod.ApiClient(base_url="http://registry.invalid")
    c.login("a@b.co", "pw")
    c.register("a@b.co", "pw")
    assert seen == [("POST", "/v1/auth/user/login", {"email": "a@b.co", "password": "pw"}), ("POST", "/v1/auth/user/register", {"email": "a@b.co", "password": "pw"})]


def test_unknown_page_is_a_404_not_a_landing_redirect(dash):
    _, client = dash
    r = client.get("/no-such-page-xyz")
    assert r.status_code == 404 and b"Page Not Found" in r.data
