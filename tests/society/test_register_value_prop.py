import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "services/dashboard/app/templates/register.html"


def _html():
    return TEMPLATE.read_text(encoding="utf-8")


def _block():
    m = re.search(r'<ul[^>]*id="account-benefits"[^>]*>(.*?)</ul>', _html(), re.S)
    assert m, "account-benefits list is missing"
    return m.group(1)


def test_benefits_list_has_three_items():
    assert len(re.findall(r"<li\b", _block())) >= 3


def test_benefits_list_comes_before_the_form():
    html = _html()
    assert html.index('id="account-benefits"') < html.index("<form")


def test_benefits_block_is_static_and_safe():
    block = _block().lower()
    for bad in ("<script", "<iframe", "<form", "http://", "https://", "|safe", "{{"):
        assert bad not in block
    assert not re.search(r"\bon[a-z]+\s*=", block)


def test_form_is_unchanged():
    html = _html()
    assert 'id="register-email"' in html
    assert 'id="register-password"' in html
