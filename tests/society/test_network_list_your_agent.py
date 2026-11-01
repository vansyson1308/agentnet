from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "services" / "dashboard" / "app" / "templates" / "network.html"
MARKER = 'data-testid="list-your-agent"'


def _text():
    return TEMPLATE.read_text(encoding="utf-8")


def _block():
    text = _text()
    assert MARKER in text, "network.html has no list-your-agent block"
    start = text.index(MARKER)
    end = text.index("</section>", start)
    return text[start:end]


def test_block_marker_present():
    assert MARKER in _text()


def test_block_has_relative_links():
    block = _block()
    assert 'href="/agent/register"' in block
    assert 'href="/register"' in block
    assert 'href="/marketplace"' in block


def test_block_is_static_and_safe():
    block = _block()
    for bad in ("{{", "{%", "|safe", "<script", "<form", "http://", "https://"):
        assert bad not in block, bad


def test_block_names_the_path_and_makes_no_live_claim():
    low = _block().lower()
    assert "list your agent" in low
    assert "hired" in low
    assert "escrow" in low
    assert "live" not in low


def test_block_is_outside_gateway_branch():
    text = _text()
    assert text.index(MARKER) < text.index("{% if not card %}")
