from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "services/dashboard/app/templates/marketplace.html"


def _paid_branch() -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    after = html.split("agent.price_per_task > 0", 1)[1]
    return after.split("{% else %}", 1)[0]


def test_paid_price_has_pay_hint():
    seg = _paid_branch()
    assert 'data-testid="pay-hint"' in seg


def test_pay_hint_mentions_credits_and_usdc():
    seg = _paid_branch()
    assert "USDC" in seg
    assert "credits" in seg


def test_pay_hint_is_static_text_only():
    seg = _paid_branch()
    hint = seg.split('data-testid="pay-hint"', 1)[1].split("</span>", 1)[0]
    assert "<a " not in hint
    assert "<form" not in hint
    assert "<script" not in hint
    assert "|safe" not in hint
