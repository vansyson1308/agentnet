from pathlib import Path

import jinja2

TEMPLATES = Path(__file__).resolve().parents[2] / "services" / "dashboard" / "app" / "templates"

STUB_BASE = (
    "{% block title %}{% endblock %}{% block extra_head %}{% endblock %}"
    "{% block content %}{% endblock %}"
)


def _render(prices, fee_pct=5, name="Agent"):
    env = jinja2.Environment(
        loader=jinja2.ChoiceLoader(
            [jinja2.DictLoader({"base.html": STUB_BASE}), jinja2.FileSystemLoader(str(TEMPLATES))]
        ),
        autoescape=True,
    )
    env.globals["url_for"] = lambda *a, **k: "/x"
    card = {
        "name": name,
        "description": "d",
        "supportedInterfaces": [],
        "skills": [{"id": "summarize", "description": "s"}],
    }
    return env.get_template("network_agent.html").render(
        agent_id="0a9fc8cc-5086-4e51-9b13-15f5e898f6f5",
        card=card,
        prices=prices,
        interfaces={},
        econ_uri="https://example.test/econ",
        fee_pct=fee_pct,
    )


def test_paid_skill_shows_fee_and_net():
    html = _render({"summarize": 100})
    assert "est. platform fee 5" in html
    assert "seller nets 95" in html


def test_free_skill_shows_no_fee():
    assert "platform fee" not in _render({"summarize": 0})


def test_negative_or_missing_price_shows_no_fee_and_no_error():
    assert "platform fee" not in _render({"summarize": -5})
    assert "platform fee" not in _render({})


def test_non_numeric_price_shows_no_fee_and_no_error():
    assert "platform fee" not in _render({"summarize": "abc"})


def test_name_is_escaped():
    html = _render({"summarize": 100}, name="<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in html
