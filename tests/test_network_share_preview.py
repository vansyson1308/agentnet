import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app" / "templates" / "network.html"


def _text():
    return TEMPLATE.read_text(encoding="utf-8")


def test_network_has_search_description_and_share_tags():
    t = _text()
    assert re.search(r'<meta\s+name="description"\s+content="[^"{]{40,200}"', t)
    assert re.search(r'<meta\s+property="og:title"\s+content="[^"{]{5,}"', t)
    assert re.search(r'<meta\s+property="og:description"\s+content="[^"{]{40,}"', t)
    assert 'name="twitter:card"' in t


def test_network_links_to_other_public_pages():
    t = _text()
    m = re.search(r'<nav[^>]*id="public-links"[^>]*>(.*?)</nav>', t, re.S)
    assert m, "public-links nav missing"
    nav = m.group(1)
    for href in ("/marketplace", "/metaverse", "/register"):
        assert 'href="%s"' % href in nav
    assert "http://" not in nav and "https://" not in nav
    assert "javascript:" not in nav
