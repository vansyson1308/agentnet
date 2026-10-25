"""Strong routing for builds (staging 2026-10-25: 48 priced runs, zero on the strong tier).

Two conditions kept every Builder/Architect route FAST: the Builder's wake
(``code_change.requested``) carries no ``kind``, and the worker never passed the
candidate's risk tier. And the build itself -- the AuthorPatch loop that is
most of the spend -- always ran on the default model. Now the candidate is
resolved for the decide route, and the build has its own route: a RED/NEVER
build, a bench ticket's build and a re-build after a failed build or QA run go
STRONG while the agent and correlation headroom stay above the floor; the
build's cost cap never exceeds that headroom. Grants are read, never written.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from decimal import Decimal

from services.registry.app.maintenance import activities
from services.registry.app.maintenance.activities import ScriptedActivityModel
from services.registry.app.models import AgentIntent, AgentRun, CodeCandidate
from services.registry.app.society import router
from services.registry.app.society.config import SocietySettings
from services.registry.app.society.events import emit_event
from services.registry.app.society.cognition import FakeModel
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker

from .test_build_engine import FIX, RED, SUBMIT, _candidate, _ev

D = Decimal


def _strong(s: SocietySettings) -> SocietySettings:
    return dataclasses.replace(s, model_name="fast-model", model_fast_name="fast-model", model_strong_name="strong-model", model_strong_floor_usd=D("0.30"))


# ── the pure build route ───────────────────────────────────────────────


def test_red_bench_and_rebuild_route_strong_and_a_plain_first_build_stays_fast(monkeypatch):
    s = _strong(SocietySettings())
    ok = dict(budget_remaining_usd=D("4.00"), correlation_remaining_usd=D("2.50"))
    assert router.choose_build_route(s, risk_tier="red", **ok).tier == router.STRONG
    assert router.choose_build_route(s, risk_tier="never", **ok).tier == router.STRONG
    assert router.choose_build_route(s, risk_tier="amber", bench=True, **ok).reason.startswith("build: bench ticket")
    r = router.choose_build_route(s, risk_tier="green", failed_builds=2, **ok)
    assert r.tier == router.STRONG and r.model_name == "strong-model" and "2 failed build(s)" in r.reason
    first = router.choose_build_route(s, risk_tier="amber", **ok)
    assert first.tier == router.FAST and first.model_name == "fast-model" and first.reason == "build: first build, not RED/bench -> fast"


def test_the_build_escalation_fails_closed_on_headroom_and_configuration():
    s = _strong(SocietySettings())
    low_agent = router.choose_build_route(s, risk_tier="red", budget_remaining_usd=D("0.29"), correlation_remaining_usd=D("2"))
    assert low_agent.tier == router.FAST and "agent budget 0.29 below escalation floor" in low_agent.reason
    low_corr = router.choose_build_route(s, risk_tier="red", budget_remaining_usd=D("2"), correlation_remaining_usd=D("0.10"))
    assert low_corr.tier == router.FAST and "correlation budget" in low_corr.reason
    none = router.choose_build_route(dataclasses.replace(s, model_strong_name=""), risk_tier="red", budget_remaining_usd=D("5"), correlation_remaining_usd=D("5"))
    assert none.tier == router.FAST and "strong tier not configured" in none.reason


def test_the_build_tier_is_the_trusted_classifier_over_files_allowed_or_the_recorded_tier():
    assert router.build_risk_tier(["services/registry/app/society/router.py"]) == "red"
    assert router.build_risk_tier(["docs/README.md"]) == "green"
    assert router.build_risk_tier(["docs/README.md"], recorded="red") == "red", "a recorded tier is never lowered"
    assert router.build_risk_tier([]) == "green"


def test_the_build_cost_cap_never_exceeds_the_headroom():
    assert router.build_cost_cap(D("2.00"), D("4.00"), D("2.70")) == D("2.00")
    assert router.build_cost_cap(D("2.00"), D("4.00"), D("1.20")) == D("1.20")
    assert router.build_cost_cap(D("2.00"), D("0.40"), None) == D("0.40")
    assert router.build_cost_cap(D("2.00"), None, None) == D("2.00")


def test_the_activity_model_takes_the_routed_model_name(monkeypatch):
    monkeypatch.setenv("SOCIETY_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("SOCIETY_MODEL_API_KEY", "test-not-a-key")
    monkeypatch.setenv("SOCIETY_MODEL_NAME", "claude-sonnet-5-5")
    monkeypatch.setenv("SOCIETY_MODEL_STRONG_NAME", "claude-opus-5-5")
    for k in ("SOCIETY_MODEL_BASE_URL", "LLM_BASE_URL", "SOCIETY_MODEL_CAPABILITY_PROFILE", "SOCIETY_MODEL_THINKING_MODE", "SOCIETY_MODEL_EFFORT"):
        monkeypatch.delenv(k, raising=False)
    s = SocietySettings()
    assert activities.get_activity_model(s).model_name == "claude-sonnet-5-5"
    strong = activities.get_activity_model(s, model_name="claude-opus-5-5")
    assert strong.model_name == "claude-opus-5-5"
    sent = []

    async def transport(request):
        sent.append(request["model"])
        return {"id": "m", "type": "message", "role": "assistant", "model": request["model"], "stop_reason": "end_turn",
                "content": [{"type": "text", "text": '{"action": "submit", "result": {}}'}], "usage": {"input_tokens": 10, "output_tokens": 5}}

    strong._m._transport = transport
    asyncio.run(strong.complete([{"role": "user", "content": "x"}], max_tokens=50))
    assert sent == ["claude-opus-5-5"]


# ── wired through the worker ───────────────────────────────────────────


def _routed_build(monkeypatch, db, SessionLocal, settings, cand, script):
    """A live-shaped build: no injected builder model, so the executor asks for
    the activity model by the routed name (captured; a scripted model serves it)."""
    asked = []

    def fake_get(s=None, *, model_name=None):
        asked.append(model_name)
        return ScriptedActivityModel(list(script))

    monkeypatch.setattr(activities, "get_activity_model", fake_get)
    emit_event(db, event_type="t.build", payload={}, correlation_id=cand.correlation_id, idempotency_key=f"t-build-{uuid.uuid4()}")
    db.commit()
    decision = {"decision_summary": "build it", "intents": [{"type": "BUILD_CODE_CANDIDATE", "payload": {"candidate_id": str(cand.id)}}], "sleep_for_seconds": 1}
    worker = SocietyWorker(SessionLocal, settings=settings, model=FakeModel({"builder": [decision]}), worker_id=f"w-{uuid.uuid4().hex[:6]}", telemetry_enabled=False)
    worker.routing = {"t.build": ["builder"]}
    asyncio.run(worker.run_until_idle(max_cycles=4))
    db.expire_all()
    row = db.query(AgentIntent).filter(AgentIntent.intent_type == "BUILD_CODE_CANDIDATE").order_by(AgentIntent.created_at.desc()).first()
    return row, asked


def test_a_red_build_asks_for_the_strong_model_and_records_the_build_route(db, SessionLocal, code_settings, grants_with_no_cooldown, monkeypatch):
    s = _strong(code_settings)
    report = seed_society(db)
    grants_with_no_cooldown()
    cand = _candidate(db, report)
    cand.risk_tier = "red"
    db.commit()
    row, asked = _routed_build(monkeypatch, db, SessionLocal, s, cand, [FIX, {"action": "run_tests", "args": {}}, SUBMIT])
    assert _ev(row.execution_status) == "executed", row.error
    assert asked == ["strong-model"]
    run = db.get(AgentRun, row.run_id)
    assert run.model_tier == router.STRONG and "build: RED-tier files" in run.route_reason and "(cap $" in run.route_reason
    assert row.result["result"]["engine"]["route"]["tier"] == router.STRONG


def test_a_rebuild_after_a_failed_build_goes_strong(db, SessionLocal, code_settings, grants_with_no_cooldown, monkeypatch):
    monkeypatch.setenv("MAINTENANCE_BUILDER_SAMPLES", "1")
    s = _strong(code_settings)
    report = seed_society(db)
    grants_with_no_cooldown()
    cand = _candidate(db, report)
    first, asked = _routed_build(monkeypatch, db, SessionLocal, s, cand, [RED, {"action": "run_tests", "args": {}}] + [SUBMIT] * 30)
    assert _ev(first.execution_status) == "failed" and asked == ["fast-model"], first.error
    assert db.get(AgentRun, first.run_id).route_reason.endswith("build: first build, not RED/bench -> fast (cap $0.50)"), "the failed build's route persists"
    second, asked = _routed_build(monkeypatch, db, SessionLocal, s, cand, [FIX, {"action": "run_tests", "args": {}}, SUBMIT])
    assert _ev(second.execution_status) == "executed", second.error
    assert asked == ["strong-model"] and "1 failed build(s)" in db.get(AgentRun, second.run_id).route_reason


def test_the_builder_decide_route_resolves_the_candidate_kind_and_tier(db, SessionLocal, code_settings, grants_with_no_cooldown):
    """code_change.requested carries candidate_id but no kind: the route used to read 'documentation/fixture change'."""
    s = _strong(code_settings)
    report = seed_society(db)
    grants_with_no_cooldown()
    cand = _candidate(db, report)
    worker = SocietyWorker(SessionLocal, settings=s, model=None, worker_id="w-route", telemetry_enabled=False)
    ev = emit_event(db, event_type="code_change.requested", payload={"candidate_id": str(cand.id), "title": cand.title}, correlation_id=cand.correlation_id,
                    idempotency_key=f"t-route-{uuid.uuid4()}")
    db.flush()
    from services.registry.app.models import Agent, AgentCapabilityGrant

    for role, want in (("builder", "builder: real code change"), ("architect", "architect: fast by policy")):
        agent = db.get(Agent, report.agents[role])
        grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == agent.id).one()
        run = AgentRun(id=uuid.uuid4(), agent_id=agent.id, event_id=ev.id, correlation_id=ev.correlation_id, role=role, status="running")
        route = worker._route(db, run, agent, grant, ev)
        assert route.reason == want, route.reason
    db.query(CodeCandidate).filter(CodeCandidate.id == cand.id).update({"spec": {**cand.spec, "files_allowed": ["services/registry/app/society/router.py"]}})
    route = worker._route(db, run, agent, grant, ev)
    assert route.tier == router.STRONG and route.reason == "architect: RED-tier work"
    db.rollback()


# ── the validator shows the routes and the delivery rate ───────────────


def _p5():
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location("phase5_live_routes", pathlib.Path(__file__).resolve().parents[2] / "deploy/railway/phase5_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_routes_step_prints_recorded_reasons_and_builds_report_dollars_per_delivery(db, engine, capsys):
    p5 = _p5()
    assert p5.parse_plan("routes,routes:20") == [("routes", []), ("routes", ["20"])]
    for bad in ("routes:x", "routes:1:2", "routes:1000"):
        try:
            p5.parse_plan(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    report = seed_society(db)
    for role, tier, reason, provider in (("builder", "strong", "builder: real code change | build: RED-tier files (cap $2.00)", "anthropic"),
                                         ("architect", "fast", "architect: fast by policy", "anthropic"), ("builder", "fast", "scripted", "fake")):
        ev = emit_event(db, event_type="code_change.requested", payload={}, idempotency_key=f"rt:{uuid.uuid4()}")
        db.add(AgentRun(id=uuid.uuid4(), agent_id=report.agents[role], event_id=ev.id, correlation_id=ev.correlation_id, role=role, status="completed",
                        cost_usd=D("0.10"), model_provider=provider, model_name="m", model_tier=tier, route_reason=reason))
    db.commit()
    out, conn = p5.Out(), engine.raw_connection()
    try:
        p5.step_routes(out, conn, 20)
    finally:
        conn.close()
    printed = capsys.readouterr().out
    assert out.failed == [] and "PHASE5 routes R01 PASS 2 builder/architect routes, 1 strong" in printed, printed
    assert "build: RED-tier files" in printed and "scripted" not in printed
    s = p5.cost_summary([("builder", "opus", 1.2, "d", True, True, False), ("builder", "opus", 0.8, "d", True, False, False)], [])
    assert s["builds"]["opus"]["usd_per_delivered"] == 2.0 and p5.cost_summary([("builder", "x", 1, "d", True, False, False)], [])["builds"]["x"]["usd_per_delivered"] is None
