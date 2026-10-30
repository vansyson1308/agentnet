from services.dashboard.app import main


def test_fee_split_values():
    assert main.fee_split(100) == (2, 98)
    assert main.fee_split(1) == (0, 1)
    assert main.fee_split(0) is None
    assert main.fee_split(True) is None
    assert main.fee_split(2.5) is None
    assert main.fee_split("100") is None
    assert main.fee_split(None) is None
    assert main.fee_split(100, True) is None
    assert main.fee_split(100, 101) is None


def _render(fees):
    card = {
        "name": "Bot", "description": "d",
        "supportedInterfaces": [],
        "skills": [{"id": "paid", "description": "p"}, {"id": "free", "description": "f"}],
    }
    with main.app.test_request_context("/"):
        return main.render_template(
            "network_agent.html", agent_id="a", card=card,
            prices={"paid": 100, "free": 0}, fees=fees, interfaces={}, econ_uri="u",
        )


def test_fee_text_only_for_paid_skill():
    html = _render({"paid": main.fee_split(100)})
    assert html.count("est. platform fee") == 1
    assert "est. platform fee 2 " in html
    assert "seller nets 98" in html


def test_no_fee_text_without_entries():
    assert "est. platform fee" not in _render({})
