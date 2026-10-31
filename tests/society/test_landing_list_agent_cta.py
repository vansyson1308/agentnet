from pathlib import Path
import re

LANDING = Path(__file__).resolve().parents[2] / "services/dashboard/app/templates/landing.html"


def _block() -> str:
    text = LANDING.read_text(encoding="utf-8")
    m = re.search(r'<section[^>]*id="list-your-agent"[^>]*>(.*?)</section>', text, re.S)
    assert m, "landing.html needs a section with id list-your-agent"
    return m.group(0)


def test_block_exists_and_mentions_a2a_card():
    block = _block()
    assert "A2A agent card" in block
    assert "List your agent" in block


def test_single_cta_points_to_register_route():
    block = _block()
    assert block.count('data-cta="list-agent"') == 1
    assert "url_for('register_page')" in block


def test_block_is_static_and_safe():
    block = _block().lower()
    assert "<script" not in block
    assert "http://" not in block and "https://" not in block
    assert not re.search(r'\son[a-z]+\s*=', block)
    assert 'href="#"' not in block
