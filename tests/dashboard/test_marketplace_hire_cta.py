from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "services/dashboard/app/templates/marketplace.html"


def _section() -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    marker = 'id="hire-cta"'
    assert marker in text, "marketplace.html must contain a section with id=hire-cta"
    start = text.index(marker)
    end = text.index("</section>", start)
    return text[start:end]


def test_hire_cta_section_present():
    assert _section().strip()


def test_hire_cta_names_price_reputation_and_time():
    low = _section().lower()
    assert "price" in low
    assert "reputation" in low
    assert "escrow" in low
    assert "time" in low


def test_hire_cta_is_static_and_safe():
    low = _section().lower()
    for banned in ("<script", "<form", "<iframe", "{{", "{%", "|safe", "http://", "https://"):
        assert banned not in low, banned
    assert "href=" in low
