"""Experience-quality contract gate (ADR-0010; desired_state.json browser_experience).

TRUSTED EVALUATION CRITERIA. The real dashboard app and templates, served with
stable fixture data in visual-test mode, probed in a real browser by the
deterministic Maintenance OS probe (services/registry/app/maintenance/browser.py).
Each critical public journey must:

* land on its own contracted path (no silent redirect to /landing);
* render no raw structured value (a capability badge shows a name, not a dict);
* show no error banner on a page that answered as contracted;
* meet WCAG 2.2 AA text contrast (4.5:1 / 3:1 large) on the dark theme;
* have no placeholder or dead same-origin link, no unlabelled form control;
* raise no JavaScript exception, and show a visible h1 and a landmark;
* stay within the performance budgets.

This is a contract about user-visible behaviour, not about how a page is
built: a repair changes the product, never this file (risk.py / policy.py
classify an edit here together with the product as evaluation laundering).
Held out of required CI with the rest of services/dashboard/tests/ until the
Society's repair makes it pass (scripts/ci/check_test_discovery.py HELD).
"""

from __future__ import annotations

import os
import pathlib

import pytest

pytestmark = pytest.mark.timeout(600)

ROOT = pathlib.Path(__file__).resolve().parents[3]
BLOCKING_RULES = {
    "navigation", "raw_structured_value", "unexpected_error_banner", "text_contrast", "dead_link", "form_label",
    "js_exception", "critical_content_missing", "redirect_loop", "layout_overflow", "performance",
}


@pytest.fixture(scope="module")
def journeys():
    import sys

    sys.path.insert(0, str(ROOT / "services" / "registry"))
    from app.maintenance.browser import run_journeys
    from app.maintenance.contracts import load_registry

    from deploy.maintenance.dashboard_fixture import CRITICAL_PAGES, serve_dashboard

    axe_path = os.getenv("MAINTENANCE_AXE_SOURCE", "")
    axe = pathlib.Path(axe_path).read_text(encoding="utf-8") if axe_path else None
    budgets = load_registry().get("performance_budgets").budgets
    with serve_dashboard() as base:
        results = run_journeys(base, CRITICAL_PAGES, axe_source=axe, budgets=budgets)
    return {r.page: r for r in results}


@pytest.mark.parametrize("page", ["ui_root", "landing", "metaverse", "marketplace", "network", "login", "register"])
def test_critical_journey_meets_the_experience_contract(journeys, page):
    r = journeys[page]
    assert r.status == 200, f"{page}: answered {r.status}"
    blocking = [(f.rule, f.count, f.selectors[:3], f.numbers, f.paths[:3]) for f in r.findings if f.rule in BLOCKING_RULES or (f.rule.startswith("axe:") and f.numbers.get("serious"))]
    assert not blocking, f"{page}: experience contract violations {blocking}"
