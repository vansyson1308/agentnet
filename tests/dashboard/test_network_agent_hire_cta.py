import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "services" / "dashboard" / "app" / "templates" / "network_agent.html"


def _html() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_agent_page_has_hire_cta():
    html = _html()
    assert 'data-testid="hire-cta"' in html
    assert "Hire this agent" in html


def test_hire_cta_is_relative_anchor_to_call_card():
    html = _html()
    tags = re.findall(r"<a\b[^>]*data-testid=\"hire-cta\"[^>]*>", html)
    assert len(tags) == 1
    assert 'href="#call-it"' in tags[0]
    assert 'id="call-it"' in html


def test_hire_cta_adds_no_form_or_unsafe_output():
    html = _html()
    assert "<form" not in html
    assert "|safe" not in html
    assert "prices" in html.split('data-testid="hire-cta"')[0].rsplit("net-card", 1)[-1] or "prices" in html
