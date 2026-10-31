from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app" / "templates" / "register.html"


def _text():
    return TEMPLATE.read_text(encoding="utf-8")


def _block():
    text = _text()
    start = text.find('<section id="account-benefits"')
    assert start != -1, "account-benefits section is missing"
    end = text.find("</section>", start)
    assert end != -1, "account-benefits section is not closed"
    return text[start:end + len("</section>")]


def test_benefits_section_precedes_form():
    text = _text()
    assert text.find('<section id="account-benefits"') != -1
    assert text.find('<section id="account-benefits"') < text.find("<form")


def test_benefits_has_heading_and_list_items():
    block = _block().lower()
    assert "<h2" in block
    assert block.count("<li") >= 3
    assert "escrow" in block
    assert "agent" in block


def test_benefits_block_is_static_and_safe():
    block = _block().lower()
    assert "{{" not in block and "{%" not in block
    for banned in ("<script", "<form", "<iframe", "|safe", "http://", "https://", " onclick", " onload", " onerror"):
        assert banned not in block, banned
    assert block.count("<section") == 1


def test_benefits_makes_no_earnings_claims():
    block = _block().lower()
    for banned in ("guarantee", "earn $", "profit", "refund", "usdc"):
        assert banned not in block, banned


def test_register_form_is_unchanged():
    text = _text()
    assert text.count("<form") == 1
    assert "url_for('register_page')" in text
    assert 'name="email"' in text and 'name="password"' in text
    assert 'minlength="12"' in text
