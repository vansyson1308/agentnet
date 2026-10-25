"""Cost calibration for Claude: measured spend -> caps, operator-set grant budgets.

The validator's ``costs`` step reads p50/p95 model spend per role+model (decide
runs), per build and per maintenance activity, and prints the caps that spend
implies inside the daily budget -- a recommendation, never applied. The
``budget`` step is the operator command that sets one role's grant budget;
the re-seed every deploy runs keeps it (the larger of it and the role
default), exactly as operator approval gates are unioned. No intent can
reach either. Money here is model spend, never wallets.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import pathlib
import uuid
from datetime import date
from decimal import Decimal

import pytest

from services.registry.app.models import AgentCapabilityGrant, AgentIntent, AgentRun
from services.registry.app.society import router
from services.registry.app.society.config import SocietySettings
from services.registry.app.society.events import EventType, emit_event, utcnow
from services.registry.app.society.seed import seed_society

REPO = pathlib.Path(__file__).resolve().parents[2]


def _p5():
    spec = importlib.util.spec_from_file_location("phase5_live_costs", REPO / "deploy/railway/phase5_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── the Opus floor ─────────────────────────────────────────────────────


def test_the_strong_tier_floor_is_configured_not_hardcoded(monkeypatch):
    monkeypatch.setenv("SOCIETY_MODEL_STRONG_NAME", "claude-opus-5-5")
    s = SocietySettings()
    assert s.model_strong_floor_usd == Decimal("0.05")
    kw = dict(role="builder", event_type="code_change.requested", event_payload={"kind": "code"})
    assert router.choose_route(s, budget_remaining_usd=Decimal("0.20"), **kw).tier == router.STRONG
    raised = dataclasses.replace(s, model_strong_floor_usd=Decimal("0.30"))
    route = router.choose_route(raised, budget_remaining_usd=Decimal("0.20"), **kw)
    assert route.tier == router.FAST and "below escalation floor" in route.reason
    assert router.choose_route(raised, budget_remaining_usd=Decimal("1.00"), correlation_remaining_usd=Decimal("0.29"), **kw).tier == router.FAST
    assert router.choose_route(raised, budget_remaining_usd=Decimal("1.00"), correlation_remaining_usd=Decimal("0.30"), **kw).tier == router.STRONG


# ── the plan grammar ───────────────────────────────────────────────────


def test_plan_grammar_for_costs_and_budget():
    p5 = _p5()
    assert p5.parse_plan("costs,costs:3,costs:7:6.5,budget:builder:2.50") == [("costs", []), ("costs", ["3"]), ("costs", ["7", "6.5"]), ("budget", ["builder", "2.50"])]
    for bad in ("costs:x", "costs:7:6:1", "costs:1.5", "budget:builder", "budget:builder:0", "budget:builder:-1", "budget:Builder:1", "budget:builder:100", "budget:builder:1.234"):
        with pytest.raises(ValueError):
            p5.parse_plan(bad)


# ── the recommendation is a pure function of measured spend ────────────


def _runs():
    d1, d2 = date(2026, 10, 24), date(2026, 10, 25)
    rows = [("scout", "claude-sonnet-5-5", c, d, False, False, False) for c, d in ((0.05, d1), (0.06, d1), (0.07, d2))]
    rows += [("governor", "claude-sonnet-5-5", 0.05, d2, False, False, False)] * 4
    rows += [("builder", "claude-sonnet-5-5", 0.06, d2, False, False, False)]
    rows += [("builder", "claude-sonnet-5-5", c, d2, True, ok, cap) for c, ok, cap in ((0.52, False, True), (0.31, True, False))]
    return rows


def test_cost_summary_separates_decides_builds_activities_and_days():
    p5 = _p5()
    acts = [("AuthorPatch", "claude-sonnet-5-5", 0.29, date(2026, 10, 25), True, False), ("AuthorPatch", "claude-sonnet-5-5", 0.50, date(2026, 10, 25), False, True)]
    s = p5.cost_summary(_runs(), acts)
    assert s["decide"]["scout/claude-sonnet-5-5"] == {"n": 3, "p50": 0.06, "p95": 0.07, "max": 0.07, "sum": 0.18}
    assert s["builds"]["claude-sonnet-5-5"]["n"] == 2 and s["builds"]["claude-sonnet-5-5"]["delivered"] == 1 and s["builds"]["claude-sonnet-5-5"]["cap_hits"] == 1
    assert s["role_daily"]["governor"] == {"days": 1, "p50": 0.2, "p95": 0.2}
    assert s["role_daily"]["builder"]["p95"] == 0.06, "a build is not decide spend"
    assert s["activities"]["AuthorPatch/claude-sonnet-5-5"]["cap_hits"] == 1
    assert s["days"] == {"2026-10-24": 0.11, "2026-10-25": round(0.07 + 0.2 + 0.06 + 0.52 + 0.31 + 0.29 + 0.5, 4)}


def test_recommendation_doubles_a_censored_build_and_fits_the_budget():
    p5 = _p5()
    rec = p5.recommend_costs(p5.cost_summary(_runs(), []), 6.0)
    # p95 build 0.52 hit the cap (censored) -> x2 = 1.04 -> 1.05, within 25% of $6
    assert rec["MAINTENANCE_MAX_CASE_COST_USD"] == 1.05
    assert rec["grants"]["builder"] == p5._ceil05(0.06 * 1.5 + 2 * 1.05) == 2.2
    assert rec["grants"]["scout"] == 0.25 and rec["grants"]["governor"] == 0.3
    assert rec["SOCIETY_MAX_CORRELATION_COST_USD"] == p5._ceil05(1.05 + 6 * 0.07) == 1.5
    assert rec["SOCIETY_MODEL_STRONG_FLOOR_USD"] == 0.15
    assert rec["fits_daily_budget"] and rec["grants_total"] == 2.75
    assert p5.recommend_costs(p5.cost_summary(_runs(), []), 2.0)["MAINTENANCE_MAX_CASE_COST_USD"] == 0.5, "one build never takes more than 25% of the day"
    assert p5.recommend_costs(p5.cost_summary([], []), 6.0)["grants"] == {}


# ── operator budgets survive the deploy re-seed ────────────────────────


def _grant(db, role):
    db.expire_all()
    return db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.role == role).one()


def test_the_budget_step_sets_a_grant_and_the_reseed_keeps_it(db, engine):
    p5 = _p5()
    seed_society(db)
    default = Decimal(str(_grant(db, "builder").daily_model_budget_usd))
    out, conn = p5.Out(), engine.raw_connection()
    try:
        p5.step_budget(out, conn, "builder", "2.20")
        p5.step_budget(out, conn, "nobody", "1.00")
    finally:
        conn.close()
    assert out.failed == ["budget.B01"], "an unknown role is refused"
    assert _grant(db, "builder").daily_model_budget_usd == Decimal("2.20")
    seed_society(db)  # what every deploy's preDeploy runs
    assert _grant(db, "builder").daily_model_budget_usd == Decimal("2.20"), "an operator raise survives the re-seed"
    g = _grant(db, "scout")
    g.daily_model_budget_usd = Decimal("0.10")
    db.commit()
    seed_society(db)
    assert _grant(db, "scout").daily_model_budget_usd == default, "a re-seed never leaves a grant below the role default"


def test_the_costs_step_reads_live_runs_only(db, engine, capsys):
    p5 = _p5()
    report = seed_society(db)
    agent_id = report.agents["builder"]
    for provider, cost, build in (("anthropic", "0.31", True), ("anthropic", "0.06", False), ("fake", "9.99", False)):
        ev = emit_event(db, event_type=EventType.SOCIETY_HEARTBEAT, payload={}, idempotency_key=f"hb:{uuid.uuid4()}")
        run = AgentRun(id=uuid.uuid4(), agent_id=agent_id, event_id=ev.id, correlation_id=ev.correlation_id, role="builder", status="completed",
                       completed_at=utcnow(), cost_usd=Decimal(cost), model_provider=provider, model_name="claude-sonnet-5-5")
        db.add(run)
        db.flush()
        if build:
            db.add(AgentIntent(id=uuid.uuid4(), run_id=run.id, agent_id=agent_id, seq=0, intent_type="BUILD_CODE_CANDIDATE", payload={},
                               idempotency_key=f"{run.id}:0", risk_class="low", policy_decision="allowed", execution_status="executed"))
    db.commit()
    out, conn = p5.Out(), engine.raw_connection()
    try:
        p5.step_costs(out, conn, 7, 6.0)
    finally:
        conn.close()
    printed = capsys.readouterr().out
    assert out.failed == [], printed
    assert '"builder/claude-sonnet-5-5": {"n": 1, "p50": 0.06' in printed.replace("\n", " ") or "builder/claude-sonnet-5-5" in printed
    assert "9.99" not in printed, "a scripted/fake run is never cost evidence"
    assert "PHASE5 costs C01 PASS 2 priced live runs" in printed
