import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "services/dashboard/app/templates/marketplace.html"


def _block() -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    m = re.search(r'<section[^>]*id="hire-example"[^>]*>.*?</section>', html, re.S)
    assert m, "marketplace.html needs a <section id=\"hire-example\">"
    return m.group(0)


def test_hire_example_block_exists_and_explains_escrow():
    block = _block().lower()
    assert "escrow" in block
    assert "hire" in block
    assert "<li" in block


def test_hire_example_links_are_relative_only():
    block = _block()
    assert 'href="/network"' in block
    assert 'href="/register"' in block


def test_hire_example_is_literal_static_markup():
    block = _block()
    low = block.lower()
    for banned in ("<script", "<form", "<iframe", "|safe", "{{", "{%", "http://", "https://"):
        assert banned not in low, banned
    assert not re.search(r"\son\w+\s*=", low)
    assert low.count("<section") == 1


def test_hire_example_precedes_the_agent_listing_script_free():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert html.index('id="hire-example"') < html.index("</body>")
