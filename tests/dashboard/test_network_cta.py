from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "services/dashboard/app/templates/network.html"
MARKER = 'data-network-cta="register"'


def _text():
    return TEMPLATE.read_text(encoding="utf-8")


def _block(text):
    assert text.count(MARKER) == 1
    idx = text.index(MARKER)
    start = text.rfind("<a", 0, idx)
    end = text.find("</a>", idx)
    assert start != -1 and end != -1
    return text[start:end + 4]


def test_cta_exists_once_with_literal_register_link():
    block = _block(_text())
    assert 'href="/register"' in block
    assert "List your agent" in block


def test_cta_block_is_static():
    block = _block(_text()).lower()
    for bad in ("{{", "{%", "|safe", "markup", "<script", "<form", "<iframe", "onclick", "http"):
        assert bad not in block


def test_cta_visible_regardless_of_gateway_state():
    text = _text()
    assert text.index(MARKER) < text.index("{% if not card %}")


def test_existing_agent_links_kept():
    text = _text()
    assert "network_agent_page" in text
    assert "Marketplace agents over A2A" in text
