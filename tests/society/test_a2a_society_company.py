"""Society A2A intents + autonomous company mode (ADR-0009 D14, D15).

Drives the REAL worker (policy -> executor -> federation pump) with a
FakeModel: the model only proposes; flags, the discovery allowlist, budgets,
breakers, approval gates, the credential boundary and the incident freeze
decide. The outbound leg talks to the official reference agent over a real
socket.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import socket
import threading
import time
import uuid
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet

from services.registry.app.a2a.federation import catalog
from services.registry.app.a2a.orm import A2AConnection, A2AOutboundCall
from services.registry.app.models import AgentCapabilityGrant, AgentIntent, CompanyCycle, ImprovementProposal, SocietyEvent
from services.registry.app.society import approvals as ap
from services.registry.app.society import company
from services.registry.app.society.cognition import FakeModel
from services.registry.app.society.config import SocietySettings, reset_settings_cache
from services.registry.app.society.context import build_context
from services.registry.app.society.events import EventType, emit_event, utcnow
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker

from .conftest import auth

REPO = pathlib.Path(__file__).resolve().parents[2]


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def reference_agent():
    import uvicorn

    spec = importlib.util.spec_from_file_location("reference_helloworld", REPO / "scripts" / "a2a" / "reference" / "reference_helloworld.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    port = _port()
    url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(mod.build_app(url), host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield url
    server.should_exit = True
    t.join(10)


@pytest.fixture
def a2a_flags(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("A2A_FEDERATION_ENABLED", "true")
    monkeypatch.setenv("A2A_SOCIETY_CLIENT_ENABLED", "true")
    monkeypatch.setenv("A2A_FEDERATION_TEST_PRIVATE_HOSTS", "127.0.0.1")
    monkeypatch.setenv("A2A_SOCIETY_DISCOVERY_ALLOWED_HOSTS", "agents.partner.example")
    monkeypatch.setenv("A2A_CREDENTIAL_KEY", Fernet.generate_key().decode())


def _settings(monkeypatch, **env) -> SocietySettings:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    return SocietySettings()


def _run_governor(db, SessionLocal, settings, grants_with_no_cooldown, intents, *, event_type="t.a2a", approval_off=True):
    report = seed_society(db)
    grants_with_no_cooldown()
    if approval_off:
        g = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == report.agents["governor"]).first()
        g.approval_required_intents = []
        db.commit()
    model = FakeModel({"Society_Governor": [{"decision_summary": "use a vendor", "intents": intents}]})
    emit_event(db, event_type=event_type)
    db.commit()
    worker = SocietyWorker(SessionLocal, settings=settings, model=model, worker_id="w", telemetry_enabled=False)
    worker.routing = {**worker.routing, event_type: ["governor"]}
    asyncio.run(worker.run_until_idle(max_cycles=12))
    return report, model, worker


def _intent(db, itype):
    db.expire_all()
    return db.query(AgentIntent).filter(AgentIntent.intent_type == itype).order_by(AgentIntent.created_at.desc()).first()


# ── policy: flags, allowlist, credential boundary ───────────────────────


def test_a2a_intents_are_denied_while_the_client_flag_is_off(db, SessionLocal, society_settings, grants_with_no_cooldown, monkeypatch):
    monkeypatch.delenv("A2A_SOCIETY_CLIENT_ENABLED", raising=False)
    _run_governor(db, SessionLocal, society_settings, grants_with_no_cooldown, [{"type": "DISCOVER_A2A_AGENT", "payload": {"card_url": "https://agents.partner.example", "reason": "r"}}])
    row = _intent(db, "DISCOVER_A2A_AGENT")
    assert _ev(row.execution_status) == "denied" and "A2A_SOCIETY_CLIENT_ENABLED" in row.policy_reason
    assert db.query(A2AOutboundCall).count() == 0


def test_the_model_cannot_point_discovery_at_an_arbitrary_host(db, SessionLocal, society_settings, grants_with_no_cooldown, a2a_flags):
    _run_governor(db, SessionLocal, society_settings, grants_with_no_cooldown, [
        {"type": "DISCOVER_A2A_AGENT", "payload": {"card_url": "https://169.254.169.254/latest", "reason": "r"}},
        {"type": "DISCOVER_A2A_AGENT", "payload": {"card_url": "http://agents.partner.example", "reason": "plain http"}},
    ])
    db.expire_all()
    rows = db.query(AgentIntent).filter(AgentIntent.intent_type == "DISCOVER_A2A_AGENT").all()
    assert {_ev(r.execution_status) for r in rows} <= {"denied", "invalid"} and len(rows) == 2
    assert db.query(A2AOutboundCall).count() == 0


def test_credential_like_input_never_leaves(db, SessionLocal, society_settings, grants_with_no_cooldown, a2a_flags):
    _run_governor(db, SessionLocal, society_settings, grants_with_no_cooldown, [
        {"type": "REQUEST_A2A_TASK", "payload": {"connection_id": str(uuid.uuid4()), "skill_id": "s", "input": {"api_key": "x"}, "reason": "exfiltrate"}},
    ])
    row = _intent(db, "REQUEST_A2A_TASK")
    assert _ev(row.execution_status) == "denied" and "credential" in row.policy_reason


# ── the full vendor flow against the official reference agent ───────────


def test_society_uses_the_reference_agent_as_a_vendor_under_approval(db, SessionLocal, society_settings, grants_with_no_cooldown, a2a_flags, reference_agent, make_user, monkeypatch):
    from services.registry.app.a2a import routes as a2a_routes

    monkeypatch.setattr(a2a_routes.runtime, "session_factory", SessionLocal)
    view = asyncio.run(catalog.discover(SessionLocal, reference_agent, {"source": "operator"}))
    op = make_user("op@company.test")
    catalog.set_state(db, uuid.UUID(view["id"]), "verified", "reference agent", op.id)
    from services.registry.app.a2a.federation import vault

    conn = A2AConnection(id=uuid.uuid4(), remote_agent_id=uuid.UUID(view["id"]), label="reference", auth_scheme="bearer", sealed_credential=vault.seal("REMOTE-SECRET-TOKEN"), daily_call_limit=5)
    db.add(conn)
    db.commit()

    intent = {"type": "REQUEST_A2A_TASK", "payload": {"connection_id": str(conn.id), "skill_id": "echo_bot", "input": {"text": "status report please"}, "reason": "vendor check"}}
    report, model, worker = _run_governor(db, SessionLocal, society_settings, grants_with_no_cooldown, [intent], approval_off=False)
    row = _intent(db, "REQUEST_A2A_TASK")
    assert _ev(row.execution_status) == "awaiting_approval", row.policy_reason  # external spend needs a human
    assert db.query(A2AOutboundCall).count() == 0

    # the credential never reached the model's context
    for ctx in model.calls:
        text = json.dumps(ctx.to_dict(), default=str)
        assert "REMOTE-SECRET-TOKEN" not in text and "gAAAA" not in text
        assert ctx.federation["connections"][0]["connection_id"] == str(conn.id)

    ap.decide(db, intent_id=row.id, user=op, decision="approved", reason="ok")
    asyncio.run(worker.run_until_idle(max_cycles=12))
    for _ in range(20):  # the pump polls the remote task (reads only) until terminal
        db.expire_all()
        call = db.query(A2AOutboundCall).one()
        if call.status in ("succeeded", "failed"):
            break
        asyncio.run(worker.pump_federation())
        time.sleep(0.2)
    assert call.status == "succeeded" and call.remote_state == "TASK_STATE_COMPLETED"
    assert "Hello, World! I have received your request (status report please)" in call.result_summary["artifactText"]
    requested = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.A2A_TASK_REQUESTED).one()
    finished = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.A2A_TASK_FINISHED).one()
    assert finished.causation_id == requested.id and finished.correlation_id == requested.correlation_id
    assert finished.payload["untrusted_external_data"] is True
    # the result wakes the Governor with the payload wrapped as untrusted data
    grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == report.agents["governor"]).first()
    from services.registry.app.models import Agent

    ctx = build_context(db, agent=db.get(Agent, report.agents["governor"]), grant=grant, event=finished, run=None, settings=society_settings)
    assert ctx.event["payload"]["_untrusted"] is True


def test_agent_chain_breaker_and_no_blind_resend(db, SessionLocal, grants_with_no_cooldown, a2a_flags, monkeypatch, make_user):
    settings = _settings(monkeypatch, SOCIETY_RUNTIME_ENABLED="true", A2A_SOCIETY_MAX_CALLS_PER_CORRELATION="1", SOCIETY_MODEL_PROVIDER="scripted")
    from services.registry.app.a2a.orm import A2ARemoteAgent

    remote = A2ARemoteAgent(id=uuid.uuid4(), card_url="https://r.example/.well-known/agent-card.json", host="r.example", state="verified", skills=[{"id": "s"}], capabilities={"interfaces": []}, card_hash="h")
    db.add(remote)
    db.flush()
    conn = A2AConnection(id=uuid.uuid4(), remote_agent_id=remote.id, label="r", auth_scheme="none", daily_call_limit=10)
    db.add(conn)
    db.commit()
    monkeypatch.setattr("services.registry.app.society.worker.SocietyWorker.pump_federation", lambda self: asyncio.sleep(0))
    mk = lambda n: {"type": "REQUEST_A2A_TASK", "payload": {"connection_id": str(conn.id), "skill_id": "s", "input": {"n": n}, "reason": "r"}}  # noqa: E731
    _run_governor(db, SessionLocal, settings, grants_with_no_cooldown, [mk(1), mk(2)])
    db.expire_all()
    rows = db.query(AgentIntent).filter(AgentIntent.intent_type == "REQUEST_A2A_TASK").order_by(AgentIntent.seq).all()
    assert [_ev(r.execution_status) for r in rows] == ["executed", "failed"]
    assert "agent-chain breaker" in rows[1].error
    # a claimed-then-interrupted call is failed, never resent
    call = db.query(A2AOutboundCall).one()
    call.status = "sent"
    call.created_at = utcnow() - timedelta(minutes=30)
    db.commit()
    from services.registry.app.society import federation_pump

    counts = asyncio.run(federation_pump.pump_once(SessionLocal))
    db.expire_all()
    assert counts["interrupted"] == 1 and db.query(A2AOutboundCall).one().status == "failed"


# ── company mode ────────────────────────────────────────────────────────


def test_company_cycle_cadence_is_once_per_day_plus_operator_invocations(db, SessionLocal, monkeypatch, make_user):
    settings = _settings(monkeypatch, SOCIETY_RUNTIME_ENABLED="true", SOCIETY_COMPANY_CYCLE_ENABLED="true", SOCIETY_COMPANY_CYCLE_HOUR_UTC="0")
    make_user("private-person@example.com")
    first = company.maybe_start_scheduled_cycle(db, settings)
    assert first is not None
    assert company.maybe_start_scheduled_cycle(db, settings) is None  # idempotent per UTC date
    extra = company.start_cycle(db, settings, trigger="operator")
    assert extra is not None and db.query(CompanyCycle).count() == 2
    ev = db.query(SocietyEvent).filter(SocietyEvent.id == first.event_id).one()
    assert ev.event_type == "company.cycle"
    text = json.dumps(ev.payload)
    assert "private-person@example.com" not in text  # aggregates only
    assert ev.payload["evidence"]["signup_funnel"]["signups"] >= 1
    assert "no high-value change" in ev.payload["instructions"].lower()


def test_kill_switch_stops_cycles(db, monkeypatch):
    settings = _settings(monkeypatch, SOCIETY_RUNTIME_ENABLED="false", SOCIETY_COMPANY_CYCLE_ENABLED="true", SOCIETY_COMPANY_CYCLE_HOUR_UTC="0")
    assert company.maybe_start_scheduled_cycle(db, settings) is None


def test_a_cycle_that_finds_nothing_records_no_high_value_change(db, SessionLocal, grants_with_no_cooldown, monkeypatch):
    settings = _settings(monkeypatch, SOCIETY_RUNTIME_ENABLED="true", SOCIETY_COMPANY_CYCLE_ENABLED="true", SOCIETY_COMPANY_CYCLE_HOUR_UTC="0", SOCIETY_MODEL_PROVIDER="scripted")
    seed_society(db)
    grants_with_no_cooldown()
    cycle = company.start_cycle(db, settings, trigger="operator")
    model = FakeModel({})  # every role: "nothing to do"
    worker = SocietyWorker(SessionLocal, settings=settings, model=model, worker_id="w", telemetry_enabled=False)
    asyncio.run(worker.run_until_idle(max_cycles=10))
    woken = {c.agent["name"] for c in model.calls}
    assert {"Society_Governor", "Society_Scout"} <= woken
    assert company.settle_cycles(db, now=utcnow() + timedelta(minutes=31)) >= 1
    db.expire_all()
    assert db.get(CompanyCycle, cycle.id).outcome == "no_high_value_change"


def test_portfolio_cap_limits_active_hypotheses(db, SessionLocal, grants_with_no_cooldown, monkeypatch):
    settings = _settings(monkeypatch, SOCIETY_RUNTIME_ENABLED="true", SOCIETY_COMPANY_CYCLE_ENABLED="true", SOCIETY_COMPANY_MAX_ACTIVE_HYPOTHESES="1", SOCIETY_MODEL_PROVIDER="scripted")
    seed_society(db)
    grants_with_no_cooldown()
    props = [{"type": "CREATE_IMPROVEMENT", "payload": {"title": f"H{i}", "problem": "p", "proposed_change": "c", "importance": 60}} for i in range(2)]
    model = FakeModel({"Society_Scout": [{"decision_summary": "two ideas", "intents": props}]})
    emit_event(db, event_type="t.cap")
    db.commit()
    worker = SocietyWorker(SessionLocal, settings=settings, model=model, worker_id="w", telemetry_enabled=False)
    worker.routing = {**worker.routing, "t.cap": ["scout"]}
    asyncio.run(worker.run_until_idle(max_cycles=10))
    db.expire_all()
    rows = db.query(AgentIntent).filter(AgentIntent.intent_type == "CREATE_IMPROVEMENT").order_by(AgentIntent.seq).all()
    assert [_ev(r.execution_status) for r in rows] == ["executed", "failed"] and "portfolio full" in rows[1].error
    assert db.query(ImprovementProposal).count() == 1


def _proposal(db, agent_id, status, *, title, updated_at=None, user_id=None):
    from services.registry.app.models import ProposalScope, ProposalSource, ProposalStatus

    extra = {"created_at": updated_at, "updated_at": updated_at} if updated_at is not None else {}
    row = ImprovementProposal(
        id=uuid.uuid4(), proposed_by_agent_id=agent_id, proposed_by_user_id=user_id, source=ProposalSource.AUDIT,
        title=title, status=ProposalStatus(status), target_scope=ProposalScope.PLATFORM, importance=50, **extra,
    )
    db.add(row)
    db.flush()
    return row


def _candidate(db, proposal, status, *, promotion=None):
    from services.registry.app.models import CodeCandidate, CodePromotion

    cand = CodeCandidate(id=uuid.uuid4(), correlation_id=uuid.uuid4(), title=f"c-{proposal.title}", spec={}, status=status, proposal_id=proposal.id)
    db.add(cand)
    db.flush()
    start = utcnow() - timedelta(hours=1)
    for i, promo in enumerate([promotion] if isinstance(promotion, str) else (promotion or [])):
        db.add(CodePromotion(id=uuid.uuid4(), candidate_id=cand.id, correlation_id=cand.correlation_id, risk_tier="green",
                             provider="fake", status=promo, created_at=start + timedelta(minutes=i)))
        db.flush()
    return cand


def test_portfolio_counts_only_hypotheses_still_being_pursued(db, monkeypatch):
    """Regression (staging 2026-09-26): the cap counted CONVERTED_TO_TASK rows
    whose candidate had merged or been rejected, and APPROVED rows nobody
    touched for a week, so the portfolio was permanently full and the Scout
    could not open a hypothesis for a critical production regression. The cap
    is unchanged; only work still being pursued holds a slot."""
    from services.registry.app.models import Agent

    settings = _settings(monkeypatch, SOCIETY_COMPANY_CYCLE_ENABLED="true", SOCIETY_COMPANY_MAX_ACTIVE_HYPOTHESES="3")
    seed_society(db)
    scout = db.query(Agent).filter(Agent.name == "Society_Scout").one().id
    old = utcnow() - timedelta(days=7)
    rows = {
        "proposed": _proposal(db, scout, "PROPOSED", title="p"),
        "under_review": _proposal(db, scout, "UNDER_REVIEW", title="u"),
        "approved_fresh": _proposal(db, scout, "APPROVED", title="af"),
        "approved_stale": _proposal(db, scout, "APPROVED", title="as", updated_at=old),
        "merged": _proposal(db, scout, "CONVERTED_TO_TASK", title="m"),
        "rejected": _proposal(db, scout, "CONVERTED_TO_TASK", title="r"),
        "ready_unpromoted": _proposal(db, scout, "CONVERTED_TO_TASK", title="ru"),
        "one_still_building": _proposal(db, scout, "CONVERTED_TO_TASK", title="b"),
        "no_linked_work": _proposal(db, scout, "CONVERTED_TO_TASK", title="n", updated_at=old),
        "promotion_retried": _proposal(db, scout, "CONVERTED_TO_TASK", title="pr"),
        "concluded_status": _proposal(db, scout, "IMPLEMENTED", title="i"),
    }
    _proposal(db, None, "PROPOSED", title="human")  # not a Society hypothesis
    _candidate(db, rows["merged"], "ready", promotion="merged")
    _candidate(db, rows["rejected"], "rejected")
    _candidate(db, rows["ready_unpromoted"], "ready")
    _candidate(db, rows["one_still_building"], "rejected")
    _candidate(db, rows["one_still_building"], "building")
    _candidate(db, rows["promotion_retried"], "ready", promotion=["rejected", "pr_open"])  # latest decides
    db.commit()

    acc = company.portfolio_accounting(db, settings)
    name = {str(r.id): k for k, r in rows.items()}
    assert sorted(name[i] for i in acc["concluded"]) == ["merged", "rejected"]
    assert sorted(name[i] for i in acc["shelved"]) == ["approved_stale"]
    assert sorted(name[i] for i in acc["active"]) == sorted(
        ["proposed", "under_review", "approved_fresh", "ready_unpromoted", "one_still_building", "no_linked_work", "promotion_retried"]
    )
    view = company._portfolio(db, settings)
    assert view["active_hypotheses"] == 7 and view["full"] is True
    assert view["concluded_open_rows"] == 2 and view["shelved_hypotheses"] == 1
    # the company.cycle worst case (test_company_cycle_context) is measured with exactly these keys
    from .test_company_cycle_context import _payload

    assert set(view) == set(_payload()["portfolio"])
    # every status is left exactly as it was: accounting reads, it never rewrites
    db.expire_all()
    assert _ev(db.get(ImprovementProposal, rows["merged"].id).status) == "CONVERTED_TO_TASK"
    assert _ev(db.get(ImprovementProposal, rows["approved_stale"].id).status) == "APPROVED"


def test_concluded_and_shelved_work_no_longer_blocks_a_new_hypothesis(db, SessionLocal, grants_with_no_cooldown, monkeypatch):
    """The live deadlock, end to end: 2 concluded + 3 week-old approvals + 1 in
    flight used to be "6 active hypotheses (cap 3)". Now it is 1, and the
    Scout's proposal executes; the cap still refuses a 4th live hypothesis."""
    from services.registry.app.models import Agent

    settings = _settings(monkeypatch, SOCIETY_RUNTIME_ENABLED="true", SOCIETY_COMPANY_CYCLE_ENABLED="true", SOCIETY_COMPANY_MAX_ACTIVE_HYPOTHESES="3", SOCIETY_MODEL_PROVIDER="scripted")
    seed_society(db)
    grants_with_no_cooldown()
    gov = db.query(Agent).filter(Agent.name == "Society_Governor").one().id
    old = utcnow() - timedelta(days=6)
    for i in range(3):
        _proposal(db, gov, "APPROVED", title=f"stale-{i}", updated_at=old)
    _candidate(db, _proposal(db, gov, "CONVERTED_TO_TASK", title="merged"), "ready", promotion="merged")
    _candidate(db, _proposal(db, gov, "CONVERTED_TO_TASK", title="rejected"), "rejected")
    _candidate(db, _proposal(db, gov, "CONVERTED_TO_TASK", title="in-flight"), "requested")
    db.commit()
    assert company._portfolio(db, settings)["active_hypotheses"] == 1

    props = [{"type": "CREATE_IMPROVEMENT", "payload": {"title": f"H{i}", "problem": "p", "proposed_change": "c", "importance": 60}} for i in range(3)]
    model = FakeModel({"Society_Scout": [{"decision_summary": "three ideas", "intents": props}]})
    emit_event(db, event_type="t.cap2")
    db.commit()
    worker = SocietyWorker(SessionLocal, settings=settings, model=model, worker_id="w", telemetry_enabled=False)
    worker.routing = {**worker.routing, "t.cap2": ["scout"]}
    asyncio.run(worker.run_until_idle(max_cycles=10))
    db.expire_all()
    rows = db.query(AgentIntent).filter(AgentIntent.intent_type == "CREATE_IMPROVEMENT").order_by(AgentIntent.seq).all()
    assert [_ev(r.execution_status) for r in rows] == ["executed", "executed", "failed"]
    assert "portfolio full: 3 active hypotheses" in rows[2].error


def test_incident_freeze_blocks_merges_and_only_an_operator_lifts_it(db, api_client, user_token, monkeypatch):
    from services.registry.app.society import promotion as promo
    from services.registry.app.models import CodeCandidate, CodePromotion

    _, plain = user_token(None)
    _, op = user_token("operator")
    assert api_client.post("/v1/society/incidents", headers=auth(plain), json={"reason": "x"}).status_code == 403
    r = api_client.post("/v1/society/incidents", headers=auth(op), json={"reason": "prod 5xx spike", "source": "operator"})
    assert r.status_code == 201
    inc_id = r.json()["id"]
    settings = _settings(monkeypatch, SOCIETY_RUNTIME_ENABLED="false")
    cand = CodeCandidate(id=uuid.uuid4(), correlation_id=uuid.uuid4(), title="t", spec={})
    promo_row = CodePromotion(id=uuid.uuid4(), candidate_id=cand.id, previous_good_sha="abc") if hasattr(CodePromotion, "previous_good_sha") else None
    reasons = promo.merge_freeze_reasons(db, settings, promo_row, cand)
    assert any(x.startswith("incident_freeze_open") for x in reasons)
    assert api_client.post(f"/v1/society/incidents/{inc_id}/lift", headers=auth(plain), json={"reason": "fine"}).status_code == 403
    assert api_client.post(f"/v1/society/incidents/{inc_id}/lift", headers=auth(op), json={"reason": "resolved"}).json()["liftedAt"]
    db.expire_all()
    assert not any(x.startswith("incident_freeze_open") for x in promo.merge_freeze_reasons(db, settings, promo_row, cand))


def test_company_status_is_operator_only_and_the_kill_switch_wins(db, api_client, user_token, monkeypatch):
    _, plain = user_token(None)
    _, op = user_token("operator")
    assert api_client.get("/v1/society/company").status_code == 401
    assert api_client.get("/v1/society/company", headers=auth(plain)).status_code == 403
    r = api_client.get("/v1/society/company", headers=auth(op))
    assert r.status_code == 200
    body = r.json()
    assert body["mode"]["production_deploy_enabled"] is False
    assert set(body) >= {"cycles", "portfolio", "evidence", "fitness", "budgets", "incidents", "release_ready_candidates"}
    for secret in ("SOCIETY_MODEL_API_KEY", "sealed_credential", "A2A_CREDENTIAL_KEY"):
        assert secret not in r.text
    monkeypatch.setenv("SOCIETY_RUNTIME_ENABLED", "false")
    reset_settings_cache()
    assert api_client.post("/v1/society/company/cycles", headers=auth(op)).status_code == 409
