import re
from pathlib import Path

TEMPLATE = Path("services/dashboard/app/templates/marketplace.html")


def _text():
    return TEMPLATE.read_text(encoding="utf-8")


def _nav():
    m = re.search(r'<nav[^>]*id="public-links"[^>]*>(.*?)</nav>', _text(), re.S)
    assert m, "nav#public-links missing"
    return m.group(1)


def test_share_preview_tags_present():
    t = _text()
    assert 'property="og:title"' in t
    assert 'property="og:description"' in t
    assert 'property="og:type"' in t
    assert 'name="twitter:card"' in t


def test_title_and_description_kept():
    t = _text()
    assert "<title>" in t
    assert 'name="description"' in t


def test_public_links_nav_targets():
    nav = _nav()
    for endpoint in ("metaverse_page", "network_page", "landing_page", "register_page"):
        assert "url_for('%s')" % endpoint in nav


def test_public_links_nav_is_static_and_safe():
    nav = _nav()
    low = nav.lower()
    assert "<script" not in low
    assert "<form" not in low
    assert "<iframe" not in low
    assert "|safe" not in nav
    assert not re.search(r"\son[a-z]+\s*=", low)
    assert "http://" not in low and "https://" not in low
    assert "<nav" not in low
