"""The deterministic browser probe detects each experience rule on planted
defects, passes a clean page, and returns structure only (ADR-0010 D10).

Needs Playwright's pinned Chromium (CI: ``python -m playwright install
--with-deps chromium``); axe-core is exercised when MAINTENANCE_AXE_SOURCE
points at the pinned build (CI installs ``axe-core@4.10.3``).
"""

from __future__ import annotations

import http.server
import os
import pathlib
import threading

import pytest

from services.registry.app.maintenance.browser import run_journeys

pytestmark = pytest.mark.timeout(600)

CLEAN = """<!doctype html><html lang="en"><head><title>ok</title><style>body{background:#0f1419;color:#e6e8eb;font:16px sans-serif}a{color:#8ab4f8}</style></head>
<body><nav><a href="/clean">Home</a></nav><main><h1>Marketplace</h1><ul><li class="cap">echo</li></ul>
<label for="q">Search</label><input id="q" name="q"><button>Go</button></main></body></html>"""

BROKEN = """<!doctype html><html lang="en"><head><title>bad</title><style>body{background:rgb(5,9,20);color:rgb(0,0,0);font:16px sans-serif}.wide{width:3000px}</style></head>
<body><nav><a href="#">Login</a><a href="/missing">Docs</a></nav><main><h1>Marketplace</h1>
<div class="alert alert-warning">Page not found.</div>
<span class="trust-badge">{'name': 'echo', 'price': 1}</span>
<input name="email"><div class="wide">x</div></main>
<script>throw new Error("boom")</script></body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        pages = {"/clean": CLEAN, "/broken": BROKEN}
        body = pages.get(self.path.split("?")[0])
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _axe():
    p = os.getenv("MAINTENANCE_AXE_SOURCE", "")
    return pathlib.Path(p).read_text(encoding="utf-8") if p else None


def test_clean_page_has_no_findings(site):
    [r] = run_journeys(site, [{"name": "clean", "path": "/clean", "expected_final_path": "/clean"}], axe_source=_axe())
    assert r.status == 200 and r.ok, [(f.rule, f.count) for f in r.findings]


def test_every_planted_defect_is_found_structurally(site):
    [r] = run_journeys(site, [{"name": "broken", "path": "/broken", "expected_final_path": "/broken"}], axe_source=_axe())
    rules = {f.rule: f for f in r.findings}
    for rule in ("text_contrast", "raw_structured_value", "unexpected_error_banner", "dead_link", "form_label", "layout_overflow", "js_exception"):
        assert rule in rules, (rule, sorted(rules))
    assert rules["raw_structured_value"].selectors == ["span.trust-badge"]
    assert rules["text_contrast"].numbers["worst_ratio"] < 1.2
    assert "/missing" in rules["dead_link"].paths
    dump = repr(r.as_dict())
    assert "Page not found" not in dump and "'name': 'echo'" not in dump and "boom" not in dump, "no page text leaves the browser"


def test_axe_core_runs_when_the_pinned_build_is_present(site):
    axe = _axe()
    if axe is None:
        pytest.skip("axe-core not installed (MAINTENANCE_AXE_SOURCE unset)")
    [r] = run_journeys(site, [{"name": "broken", "path": "/broken"}], axe_source=axe)
    assert any(f.rule == "axe:color-contrast" for f in r.findings)


def test_findings_become_separate_structural_incidents(db, mset, site):
    from services.registry.app.maintenance.browser_ingest import BrowserReport, ingest_browser
    from services.registry.app.maintenance.orm import MaintenanceIncident

    results = run_journeys(site, [{"name": "broken", "path": "/broken"}, {"name": "clean", "path": "/clean"}])
    report = BrowserReport.model_validate({"target": "staging", "pages": [{k: v for k, v in r.as_dict().items() if k != "ok"} for r in results]})
    ingest_browser(db, mset, report)
    db.commit()
    refs = {i.desired_state_ref: i.incident_class for i in db.query(MaintenanceIncident).all()}
    assert refs["broken:text_contrast"] == "ACCESSIBILITY" and refs["broken:raw_structured_value"] == "UI_RENDERING" and refs["broken:dead_link"] == "UI_NAVIGATION"
    assert not any(r.startswith("clean:") for r in refs), "one incident per (page, rule); unrelated defects are never bundled"
