import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "services/dashboard/app/templates/landing.html"


def _text():
    return TEMPLATE.read_text(encoding="utf-8")


def test_exactly_one_primary_cta_marker():
    assert _text().count("data-primary-cta") == 1


def test_primary_cta_links_to_marketplace():
    m = re.search(r"<a[^>]*data-primary-cta[^>]*>", _text())
    assert m, "primary CTA anchor missing"
    assert "url_for('marketplace_page')" in m.group(0)


def test_features_section_is_labelled():
    t = _text()
    assert 'id="how-it-works"' in t
    assert 'aria-label="How it works"' in t


def test_no_unsafe_rendering_added():
    t = _text()
    assert "|safe" not in t
    assert "Markup(" not in t
