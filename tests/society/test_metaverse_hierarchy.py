from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "services"
    / "dashboard"
    / "app"
    / "templates"
    / "metaverse.html"
)


def _text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_exactly_one_primary_cta():
    assert _text().count("data-primary-cta") == 1


def test_primary_cta_links_by_login_state():
    text = _text()
    assert "url_for('marketplace_page')" in text
    assert "url_for('login_page')" in text


def test_agent_directory_section_is_labelled():
    text = _text()
    assert 'id="agent-directory"' in text
    assert "aria-labelledby" in text


def test_template_blocks_balanced():
    text = _text()
    assert text.count("{% block ") == text.count("{% endblock %}")
    assert "Metaverse Command Center" in text
