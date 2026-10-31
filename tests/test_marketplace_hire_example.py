from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app" / "templates" / "marketplace.html"


def _text():
    return TEMPLATE.read_text(encoding="utf-8")


def test_hire_example_section_present():
    text = _text()
    assert 'id="hire-example"' in text
    low = text.lower()
    assert "escrow" in low
    assert "hire" in low
    assert "<li" in low


def test_hire_example_links_are_relative_only():
    text = _text()
    start = text.index('id="hire-example"')
    block = text[start:start + 3000]
    assert 'href="/network' in block
    assert 'href="/register' in block
    assert "http://" not in block and "https://" not in block


def test_hire_example_block_is_static_markup():
    text = _text()
    start = text.index('id="hire-example"')
    end = text.find("</section>", start)
    assert end != -1
    block = text[start:end].lower()
    for banned in ("<script", "<form", "<iframe", "{{", "{%", "|safe", " onclick=", " onload="):
        assert banned not in block
