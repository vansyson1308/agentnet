"""The public-surface contract, applied to the dashboard in-process.

These are the TRUSTED acceptance gates for a dashboard repair: the public
surface contract, route integrity, no link-masking fallback, no truncation
markers, the anonymous route law and a bounded navigation crawl. They run the
SAME checks as the Society's production monitor (services/registry/app/society/
surface.py) through ``httpx.WSGITransport``, so a missing page that redirects
to the landing page, a '#' placeholder link, a missing content marker or a
template ``url_for`` that names no route fails here exactly as it fails in
production. docs/PUBLIC_SURFACE_CONTRACT.md explains each gate.

A candidate that changes the dashboard must not edit this file or the
contract in the same change (QA's no-self-judging check; risk.py treats both
as evaluation criteria).

The registry is stubbed at ``ApiClient._request``: no network, deterministic
public data, and any path the stub does not know answers 404.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
DASH = ROOT / "services" / "dashboard" / "app"
TEMPLATES = DASH / "templates"
SURFACE_PY = ROOT / "services" / "registry" / "app" / "society" / "surface.py"
UI = "http://dashboard.test"
API = "http://api.test"

AGENT_ID = "0b7d6c1e-0000-4000-8000-000000000001"
AGENT = {
    "id": AGENT_ID, "name": "Translator", "description": "Translates text", "capabilities": [],
    "total_tasks_completed": 3, "total_tasks_failed": 0, "total_tasks_timeout": 0, "success_rate": 1.0,
    "reputation_tier": "bronze", "status": "active", "is_active": True,
}
CARD = {
    "name": "AgentNet",
    "supportedInterfaces": [
        {"url": "https://api.agentnet.io.vn/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
        {"url": "https://api.agentnet.io.vn/a2a/http", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"},
    ],
    "capabilities": {"streaming": True, "extensions": []},
    "skills": [],
}
AGENT_CARD = {**CARD, "name": "Translator", "supportedInterfaces": [{**CARD["supportedInterfaces"][0], "tenant": AGENT_ID}]}

TRUNCATION_MARKERS = re.compile(
    r"(\[TRUNCATED|preserve when editing|rest of (the )?(file|routes|methods|code)\b[^\n]{0,40}(preserved|remain|unchanged)"
    r"|(other|remaining) (methods|routes|functions) remain unchanged)",
    re.IGNORECASE,
)


def _surface():
    spec = importlib.util.spec_from_file_location("agentnet_public_surface_dash", SURFACE_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


SURFACE = _surface()
CONTRACT = SURFACE.load_contract()
UI_ITEMS = [i for i in CONTRACT.items if i.origin == "ui"]


@pytest.fixture(scope="module")
def dash():
    mp = pytest.MonkeyPatch()
    mp.setenv("ENVIRONMENT", "development")
    mp.setenv("REGISTRY_URL", "http://registry.invalid")
    from services.dashboard.app import api_client as client_mod
    from services.dashboard.app import main

    def stub(self, method, path, **kwargs):
        p = path.split("?", 1)[0]
        if method == "GET":
            if p.rstrip("/") == "/v1/agents/public":
                return [AGENT]
            if p == "/.well-known/agent-card.json":
                return CARD
            if p == "/v1/a2a/conformance":
                return {"operations": {}, "notOffered": []}
            if p == "/v1/a2a/federation/summary":
                return {"total": 0, "remoteAgentsByState": {}}
            if p == f"/v1/agents/{AGENT_ID}/a2a-card":
                return AGENT_CARD
            if p == f"/v1/agents/{AGENT_ID}":
                return AGENT
            if p in ("/health", "/healthz", "/readyz"):
                return {"status": "ok"}
        raise client_mod.APIError(f"not stubbed: {method} {p}", 404)

    mp.setattr(client_mod.ApiClient, "_request", stub)
    main.app.config.update(TESTING=True)
    yield main
    mp.undo()


def _client(main) -> httpx.Client:
    return httpx.Client(transport=httpx.WSGITransport(app=main.app), follow_redirects=False)


def _fmt(obs) -> str:
    return "; ".join(f"{o.name}: {o.failure} ({o.detail})" for o in obs)


# ── 1. the public surface contract ─────────────────────────────────────────


@pytest.mark.parametrize("item_name", [i.name for i in UI_ITEMS])
def test_contract_item(dash, item_name):
    """Status, final path after redirects and page-specific markers, per
    contract item. A landing-page 200 at the end of a redirect is a failure."""
    item = CONTRACT.item(item_name)
    with _client(dash) as c:
        obs, _ = SURFACE.check_item(c, CONTRACT, item, {"ui": UI, "api": API})
    assert obs.ok, _fmt([obs])


def test_navigation_links_forms_and_assets_resolve(dash):
    """Bounded crawl from the core pages: every same-origin link and form
    action reaches a real page (or the login page for a protected one); no
    '#'/'javascript:' placeholders; local assets load."""
    with _client(dash) as c:
        report = SURFACE.run_contract({"ui": UI, "api": API}, contract=CONTRACT, client=c, include_api=False)
    crawl = [o for o in report.failures() if o.kind in ("link", "placeholder", "asset")]
    assert not crawl, _fmt(crawl)
    assert report.unsafe_links == 0


# ── 2. route integrity ──────────────────────────────────────────────────────


def _rendered_templates() -> set:
    """Templates the dashboard's views render (string literals passed to
    render_template), plus everything they extend/include/import."""
    names = set()
    for py in DASH.glob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "render_template":
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    names.add(node.args[0].value)
    todo, seen = list(names), set()
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        seen.add(n)
        path = TEMPLATES / n
        if path.exists():
            for dep in re.findall(r"{%-?\s*(?:extends|include|import|from)\s+['\"]([^'\"]+)['\"]", path.read_text(encoding="utf-8")):
                todo.append(dep)
    return seen


def test_rendered_templates_exist_and_are_not_empty(dash):
    active = _rendered_templates()
    assert active, "the dashboard renders no templates"
    missing = sorted(n for n in active if not (TEMPLATES / n).exists())
    empty = sorted(n for n in active if (TEMPLATES / n).exists() and not (TEMPLATES / n).read_text(encoding="utf-8").strip())
    assert not missing and not empty, f"missing={missing} empty={empty}"


def test_every_active_template_url_for_names_a_route(dash):
    endpoints = set(dash.app.view_functions)
    broken = {}
    for name in sorted(_rendered_templates()):
        path = TEMPLATES / name
        if not path.exists():
            continue
        for ep in re.findall(r"url_for\(\s*['\"]([\w\.]+)['\"]", path.read_text(encoding="utf-8")):
            if ep not in endpoints:
                broken.setdefault(name, set()).add(ep)
    assert not broken, "active templates link to endpoints that do not exist: " + "; ".join(f"{k}: {sorted(v)}" for k, v in broken.items())


def test_every_active_template_literal_href_names_a_route(dash):
    adapter = dash.app.url_map.bind("dashboard.test")
    broken = {}
    for name in sorted(_rendered_templates()):
        path = TEMPLATES / name
        if not path.exists():
            continue
        for href in re.findall(r"""(?:href|action)\s*=\s*["'](/[^"'{}#?]*)""", path.read_text(encoding="utf-8")):
            if href.startswith("/static/"):
                continue
            try:
                adapter.match(href, method="GET")
            except Exception as exc:  # noqa: BLE001 -- NotFound, MethodNotAllowed, RequestRedirect
                if type(exc).__name__ not in ("MethodNotAllowed", "RequestRedirect"):
                    broken.setdefault(name, set()).add(href)
    assert not broken, "active templates hard-code paths with no route: " + "; ".join(f"{k}: {sorted(v)}" for k, v in broken.items())


def test_no_url_build_fallback_masks_missing_routes(dash):
    """A missing endpoint must fail loudly (BuildError), never render '#'."""
    assert not dash.app.url_build_error_handlers, "a url_build_error_handler masks missing routes"


def test_no_truncation_markers_in_dashboard_sources(dash):
    sources = [DASH / "main.py", DASH / "api_client.py"] + [TEMPLATES / n for n in _rendered_templates() if (TEMPLATES / n).exists()]
    hits = [str(p.relative_to(ROOT)) for p in sources if p.exists() and TRUNCATION_MARKERS.search(p.read_text(encoding="utf-8"))]
    assert not hits, f"placeholder/truncation markers left in active sources: {hits}"


# ── 3. the anonymous route law ──────────────────────────────────────────────


def _plain_get_rules(app):
    for rule in app.url_map.iter_rules():
        if "GET" not in (rule.methods or ()) or rule.arguments or rule.endpoint == "static":
            continue
        if rule.rule in ("/healthz", "/readyz") or "logout" in rule.rule:
            continue
        yield rule.rule


def test_anonymous_requests_land_on_the_page_or_on_login(dash):
    """An anonymous visitor gets the page, or an intentional redirect to the
    login page for a protected one -- never a 404/500 and never a silent
    bounce to the landing page."""
    bad = []
    with _client(dash) as c:
        for path in sorted(_plain_get_rules(dash.app)):
            r = c.get(UI + path)
            hops = 0
            while r.status_code in (301, 302, 303, 307, 308) and hops < 5:
                r = c.get(httpx.URL(UI).join(r.headers["location"]))
                hops += 1
            final = urlsplit(str(r.url)).path
            declared = CONTRACT.declared_final_path("ui", path)
            if r.status_code >= 400:
                bad.append(f"{path} -> {r.status_code}")
            elif final not in (path, declared, CONTRACT.login_path):
                bad.append(f"{path} -> ends at {final}")
    assert not bad, "; ".join(bad)


def test_unknown_pages_are_not_disguised_as_the_landing_page(dash):
    """A path that is not a page answers 404 (the body may be friendly). A
    redirect to the landing page would make every missing page look healthy
    to users, crawlers and the monitor alike."""
    with _client(dash) as c:
        r = c.get(UI + "/definitely-not-a-page-6b1f")
    assert r.status_code == 404, f"unknown path -> {r.status_code} {r.headers.get('location', '')}"


# ── 4. the human auth forms post somewhere real ─────────────────────────────


@pytest.mark.parametrize("page", ["/login", "/register"])
def test_auth_forms_post_to_a_real_handler(dash, page):
    with _client(dash) as c:
        r = c.get(UI + page)
    assert r.status_code == 200, f"{page} -> {r.status_code}"
    forms = SURFACE.extract(r.content).forms
    assert forms, f"{page} has no form"
    adapter = dash.app.url_map.bind("dashboard.test")
    for action in forms:
        target = urlsplit(action).path or page
        endpoint, _ = adapter.match(target, method="POST")
        assert endpoint in dash.app.view_functions
