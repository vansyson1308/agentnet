import re
from pathlib import Path

LANDING = (
    Path(__file__).resolve().parents[2]
    / "services" / "dashboard" / "app" / "templates" / "landing.html"
)


def _section():
    text = LANDING.read_text(encoding="utf-8")
    m = re.search(
        r'<section[^>]*id="market-activity"[^>]*>(.*?)</section>', text, re.S
    )
    assert m, "landing.html needs a section with id=market-activity"
    return m.group(1)


def test_section_exists_and_links_to_live_pages():
    body = _section()
    assert "url_for('marketplace_page')" in body
    assert "url_for('metaverse_page')" in body


def test_section_is_static_and_safe():
    body = _section().lower()
    assert "<script" not in body
    assert "<form" not in body
    assert "<iframe" not in body
    assert "http://" not in body and "https://" not in body
    assert not re.search(r'\son[a-z]+\s*=', body)
