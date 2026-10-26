"""Register-page value copy and the landing sign-up CTA (offline)."""

from __future__ import annotations

import re

import pytest


@pytest.fixture(scope="module")
def dash():
    mp = pytest.MonkeyPatch()
    mp.setenv("ENVIRONMENT", "development")
    mp.setenv("REGISTRY_URL", "http://registry.invalid")
    from services.dashboard.app import api_client as client_mod
    from services.dashboard.app import main

    def stub(self, method, path, **kwargs):
        p = path.split("?", 1)[0]
        if method == "GET" and p.rstrip("/") == "/v1/agents/public":
            return []
        raise client_mod.APIError(f"not stubbed: {method} {p}", 404)

    mp.setattr(client_mod.ApiClient, "_request", stub)
    main.app.config.update(TESTING=True)
    yield main
    mp.undo()


def _get(main, path):
    with main.app.test_client() as c:
        r = c.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    return r.get_data(as_text=True)


def _form(html):
    m = re.search(r"<form\b[^>]*>.*?</form>", html, re.S | re.I)
    assert m, "no form rendered"
    return m


def test_register_benefits_and_note_precede_form(dash):
    html = _get(dash, "/register")
    form_at = _form(html).start()
    before = html[:form_at].lower()
    assert "wallet" in before
    assert "escrow" in before
    assert "list your own agent" in before
    assert "verification email follows sign-up" in before
    assert "already registered" not in html.lower()
    assert "already exists" not in html.lower()


def test_register_form_unchanged(dash):
    html = _get(dash, "/register")
    form = _form(html)
    open_tag = re.match(r"<form\b[^>]*>", form.group(0), re.I).group(0)
    with dash.app.test_request_context():
        from flask import url_for

        register_url = url_for("register_page")
    assert f'action="{register_url}"' in open_tag
    assert re.search(r'method="post"', open_tag, re.I)
    body = form.group(0)
    assert 'name="email"' in body
    assert 'name="password"' in body
    assert 'type="submit"' in body


def test_landing_primary_cta_points_to_register(dash):
    html = _get(dash, "/")
    with dash.app.test_request_context():
        from flask import url_for

        register_url = url_for("register_page")
    anchors = re.findall(r"<a\b[^>]*btn-cta-primary[^>]*>", html)
    assert anchors, "no primary CTA on the landing page"
    href = re.search(r'href="([^"]*)"', anchors[0]).group(1)
    assert href == register_url
    assert "next=" not in href
    assert not href.lower().startswith(("http:", "https:", "//"))


@pytest.mark.parametrize("path", ["/register", "/"])
def test_no_none_or_undefined_text(dash, path):
    html = _get(dash, path)
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    assert not re.search(r"\b(None|undefined)\b", text)
