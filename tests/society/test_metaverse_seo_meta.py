from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "services" / "dashboard" / "app" / "templates" / "metaverse.html"


def _text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_search_and_share_meta_present():
    text = _text()
    assert 'name="description"' in text
    assert 'property="og:title"' in text
    assert 'property="og:description"' in text
    assert 'name="twitter:card"' in text


def test_public_links_block_has_marketplace_and_network():
    text = _text()
    marker = 'data-testid="public-links"'
    assert marker in text
    start = text.index(marker)
    end = text.index("</nav>", start)
    block = text[start:end]
    assert 'href="/marketplace"' in block
    assert 'href="/network"' in block


def test_meta_block_is_static_and_safe():
    text = _text()
    start = text.index('name="description"')
    line_end = text.index(">", start)
    assert "{{" not in text[start:line_end]
    assert "|safe" not in text
