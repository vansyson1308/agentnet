import re
from pathlib import Path

LANDING = Path(__file__).resolve().parents[2] / "services/dashboard/app/templates/landing.html"


def _html():
    return LANDING.read_text(encoding="utf-8")


def test_landing_has_list_your_agent_cta():
    html = _html()
    assert 'data-testid="list-agent-cta"' in html
    assert "List your agent" in html
    assert "A2A agent card" in html


def test_cta_is_anchor_to_register_page():
    html = _html()
    match = re.search(r'<a [^>]*data-testid="list-agent-cta"[^>]*>', html)
    assert match, "CTA must be an <a> element"
    assert "url_for('register_page')" in match.group(0)


def test_landing_stays_static_and_safe():
    html = _html()
    assert "|safe" not in html
    assert "<form" not in html
