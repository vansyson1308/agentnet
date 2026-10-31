from pathlib import Path

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parents[2] / "services" / "dashboard" / "app" / "templates"
STUB_BASE = (
    "{% block title %}{% endblock %}{% block extra_head %}{% endblock %}"
    "{% block content %}{% endblock %}"
)
HINT = "credits or USDC"


def _render(skills, prices):
    env = Environment(
        loader=ChoiceLoader([DictLoader({"base.html": STUB_BASE}), FileSystemLoader(str(TEMPLATES))]),
        autoescape=True,
    )
    env.globals["url_for"] = lambda *a, **k: "/network"
    tpl = env.get_template("network_agent.html")
    card = {"name": "Agent", "description": "d", "supportedInterfaces": [], "skills": skills}
    return tpl.render(
        agent_id="a-1", card=card, prices=prices, interfaces={}, econ_uri="https://example.test/econ"
    )


def test_paid_skill_shows_usdc_hint():
    html = _render([{"id": "paid_skill", "description": "x"}], {"paid_skill": 5})
    assert HINT in html
    assert "5" in html


def test_free_skill_does_not_show_usdc_hint():
    html = _render([{"id": "free_skill", "description": "x"}], {"free_skill": 0})
    assert HINT not in html
    assert "free" in html


def test_page_says_caller_chooses_currency_and_no_conversion():
    html = _render([{"id": "paid_skill", "description": "x"}], {"paid_skill": 5})
    assert "chosen by the caller" in html
    assert "no conversion" in html


def test_dynamic_values_stay_escaped():
    html = _render([{"id": "paid_skill", "description": "<script>alert(1)</script>"}], {"paid_skill": 5})
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
