"""The north star (goals.py): revenue toward $1M ranks the plan, reaches every role, and is real money only.

Money invariant: the revenue metrics READ the platform fee the transactions trigger took (USDC only;
credits are not money) and the operator's record-only ledger; recording external income never
changes a wallet or a transaction (``test_recording_external_income_is_record_only_and_idempotent``).
"""

from __future__ import annotations

import copy
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from services.registry.app.models import ImprovementProposal
from services.registry.app.society import backlog, charter, goals, metrics, tickets
from services.registry.app.society.config import reset_settings_cache
from services.registry.app.society.events import utcnow

from .conftest import auth
from .test_company_tickets import _activate, company as company  # noqa: F401 -- fixture


@pytest.fixture
def north_star_on(monkeypatch):
    monkeypatch.setenv("SOCIETY_NORTH_STAR_ENABLED", "true")
    reset_settings_cache()
    yield
    reset_settings_cache()


def _paid(db, make_agent, amount, currency="usdc"):
    """A completed agent-to-agent payment: the trigger takes the platform fee (2.5%)."""
    caller, callee = make_agent(f"c{uuid.uuid4().hex[:6]}", spending_cap=100000), make_agent(f"s{uuid.uuid4().hex[:6]}")
    wallets = {r[0]: r[1] for r in db.execute(text("SELECT owner_id, id FROM wallets WHERE owner_id IN (:a, :b)"), {"a": caller.id, "b": callee.id})}
    db.execute(text("UPDATE wallets SET balance_usdc = 100000, balance_credits = 100000 WHERE id = :w"), {"w": wallets[caller.id]})
    tx = uuid.uuid4()
    db.execute(text("INSERT INTO transactions (id, from_wallet, to_wallet, amount, currency, status, type) "
                    "VALUES (:id, :f, :t, :a, CAST(:c AS currency_type), 'pending', 'payment')"),
               {"id": tx, "f": wallets[caller.id], "t": wallets[callee.id], "a": amount, "c": currency})
    db.execute(text("UPDATE transactions SET status = 'completed', completed_at = NOW() WHERE id = :id"), {"id": tx})
    db.commit()
    return wallets


def test_the_charter_names_a_revenue_north_star_and_weighs_every_key_result():
    doc = charter.load()
    ns = doc["north_star"]
    assert (ns["metric_id"], ns["target"], ns["direction"]) == ("revenue_usd_total", 1000000, "up")
    krs = [kr for o in doc["objectives"] for kr in o["key_results"]]
    assert all(0 <= kr["north_star_weight"] <= 1 for kr in krs)
    ids = {o["id"]: o for o in doc["objectives"]}
    assert {kr["metric_id"] for kr in ids["O4"]["key_results"]} == {"revenue_usd_30d", "paying_callers_30d", "external_jobs_paid_30d"}
    assert ids["O4"]["status"] == ids["O5"]["status"] == "proposed", "only an operator activates an objective"

    bad = copy.deepcopy(doc)
    bad["objectives"][0]["key_results"][0]["north_star_weight"] = 1.5
    with pytest.raises(charter.CharterError, match="north_star_weight"):
        charter.validate(bad)
    bad = copy.deepcopy(doc)
    bad["north_star"]["metric_id"] = "vibes"
    with pytest.raises(charter.CharterError, match="north_star"):
        charter.validate(bad)


def test_revenue_is_usdc_platform_fees_plus_recorded_income_never_credits(db, make_agent, make_user):
    assert metrics.read(db, "revenue_usd_total") == 0.0
    _paid(db, make_agent, 400)  # fee FLOOR(400 * 0.025) = 10 USDC
    _paid(db, make_agent, 400, currency="credits")  # credits are not money
    assert metrics.read(db, "revenue_usd_30d") == 10.0 and metrics.read(db, "paying_callers_30d") == 1.0
    goals.record_revenue(db, source="external_job", amount_usd=25.5, reference="upwork:payout-1")
    goals.record_revenue(db, source="sponsorship", amount_usd=100, reference="sponsor:old", received_at=utcnow() - timedelta(days=60))
    db.commit()
    assert metrics.read(db, "revenue_usd_30d") == 35.5, "the old sponsorship is outside 30 days"
    assert metrics.read(db, "revenue_usd_total") == 135.5
    assert metrics.read(db, "external_jobs_paid_30d") == 1.0


def test_recording_external_income_is_record_only_and_idempotent(db, make_agent):
    wallets = _paid(db, make_agent, 400)
    before = db.execute(text("SELECT id, balance_usdc, balance_credits, reserved_usdc FROM wallets ORDER BY id")).all()
    txs = db.execute(text("SELECT COUNT(*) FROM transactions")).scalar()
    first = goals.record_revenue(db, source="external_job", amount_usd="40", reference="inv-7", note="logo job")
    again = goals.record_revenue(db, source="external_job", amount_usd=40.0, reference="inv-7")
    db.commit()
    assert first["id"] == again["id"] and db.execute(text("SELECT COUNT(*) FROM society_revenue_entries")).scalar() == 1
    with pytest.raises(ValueError, match="different amount"):
        goals.record_revenue(db, source="external_job", amount_usd=41, reference="inv-7")
    for bad in ({"amount_usd": 0}, {"amount_usd": -5}, {"source": "loan"}, {"reference": " "}):
        with pytest.raises(ValueError):
            goals.record_revenue(db, **{"source": "other", "amount_usd": 1, "reference": "r-x", **bad})
    assert db.execute(text("SELECT id, balance_usdc, balance_credits, reserved_usdc FROM wallets ORDER BY id")).all() == before
    assert db.execute(text("SELECT COUNT(*) FROM transactions")).scalar() == txs and wallets


def test_outside_agents_exclude_the_societys_own(db, company, make_agent):
    report, _ = company
    assert metrics.read(db, "outside_agents_joined_7d") == 0.0, f"the {len(report.agents)} society agents are not newcomers"
    make_agent("newcomer")
    assert metrics.read(db, "outside_agents_joined_7d") == 1.0


def _ticket(db, report, objective, metric, effect):
    p = ImprovementProposal(id=uuid.uuid4(), proposed_by_agent_id=report.agents["scout"], source="audit", title=f"t {uuid.uuid4().hex[:6]}",
                            problem="p", proposed_change="c", status="APPROVED", target_scope="platform", importance=60)
    db.add(p)
    db.flush()
    tid = tickets.create(db, proposal_id=p.id, title=p.title, fields={"objective_id": objective, "metric_id": metric, "expected_effect": effect,
                         "direction": "up", "proof": [f"probe:{metric}"]}, source="scout", role="scout", importance=60)
    db.commit()
    return str(tid)


def test_the_plan_ranks_by_north_star_weight_only_when_enabled(db, company, monkeypatch):
    report, _ = company
    _activate(db, "O2")
    _activate(db, "O5")
    # same owner priority (2), same share of the KR gap (half): 4 x 0.5 = 2.0;: only the weight differs (0.7 vs 0.5)
    signups = _ticket(db, report, "O5", "verified_signups_7d", 15)
    escrow = _ticket(db, report, "O2", "escrow_tasks_completed_7d", 25)
    obj = {o["id"]: o for o in charter.objectives(db)}
    t_sign, t_escrow = (tickets._ticket_ref(db, t) for t in (signups, escrow))
    assert tickets.score(t_sign, obj["O5"]) == tickets.score(t_escrow, obj["O2"]) == 2.0
    assert tickets.score(t_escrow, obj["O2"], north_star=True) == 3.4 and tickets.score(t_sign, obj["O5"], north_star=True) == 3.0

    monkeypatch.setenv("SOCIETY_NORTH_STAR_ENABLED", "true")
    reset_settings_cache()
    try:
        plan = tickets.build_plan(db, None, utcnow())
    finally:
        reset_settings_cache()
    assert [r["ticket_id"] for r in plan["ranking"]] == [escrow, signups]
    assert [r["north_star_weight"] for r in plan["ranking"]] == [0.7, 0.5]


def test_growth_supply_covers_the_revenue_key_results_only_with_the_north_star(db, company, monkeypatch):
    _activate(db, "O4")
    _activate(db, "O5")
    assert backlog.kr_gap_items(db) == [], "off: O4/O5 metrics are not supplied"
    monkeypatch.setenv("SOCIETY_NORTH_STAR_ENABLED", "true")
    reset_settings_cache()
    try:
        items = backlog.kr_gap_items(db)
    finally:
        reset_settings_cache()
    assert {i["metric_id"] for i in items} == {"revenue_usd_30d", "paying_callers_30d", "outside_agents_joined_7d", "verified_signups_7d"}, \
        "external income has no product lever: it is the owner's outreach"
    made = tickets.supply(db, items, utcnow())
    db.commit()
    from services.registry.app.society import work_packet

    for tid in made:
        t = tickets._ticket_ref(db, tid)
        assert work_packet.for_ticket(db, t)["kind"] == "growth", t["metric_id"]


def test_every_role_sees_the_scorecard_when_enabled(db, company, make_agent, north_star_on):
    from services.registry.app.society.context import _company

    _activate(db, "O4")
    _paid(db, make_agent, 400)
    view = _company(db)["north_star"]
    assert view["current_usd"] == 10.0 and view["target_usd"] == 1000000 and not view["on_pace"]
    assert view["key_results_by_weight"][0]["metric_id"] in ("revenue_usd_30d", "external_jobs_paid_30d")
    assert "not intents" in view["rule"]
    card = goals.scorecard(db)
    assert card["north_star"]["gap_usd"] == 999990.0 and "North star: $10.00 / $1,000,000" in card["summary_text"]


def test_the_scorecard_is_absent_when_disabled(db, company):
    from services.registry.app.society.context import _company

    assert "north_star" not in _company(db)


def test_only_an_operator_records_revenue_and_reads_the_north_star(db, api_client, user_token):
    _, plain = user_token(None)
    _, op = user_token("operator")
    body = {"source": "external_job", "amount_usd": 12.5, "reference": "fiverr:order-1"}
    assert api_client.post("/v1/society/company/revenue", headers=auth(plain), json=body).status_code == 403
    assert api_client.get("/v1/society/company/north-star", headers=auth(plain)).status_code == 403
    r = api_client.post("/v1/society/company/revenue", headers=auth(op), json=body)
    assert r.status_code == 201 and r.json()["amount_usd"] == 12.5
    assert api_client.post("/v1/society/company/revenue", headers=auth(op), json={**body, "amount_usd": 13}).status_code == 409
    assert api_client.post("/v1/society/company/revenue", headers=auth(op), json={**body, "amount_usd": -1}).status_code == 422
    card = api_client.get("/v1/society/company/north-star", headers=auth(op)).json()
    assert card["north_star"]["current_usd"] == 12.5 and [e["reference"] for e in card["revenue_entries"]] == ["fiverr:order-1"]
