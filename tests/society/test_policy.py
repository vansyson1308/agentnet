"""Policy engine matrix + the structural invariant that nothing in the
runtime can grant itself privileges."""

from __future__ import annotations

import inspect
import pathlib
import re
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from services.registry.app.models import AgentCapabilityGrant, AgentRun, IntentRiskClass, PolicyDecision
from services.registry.app.society import executor as executor_module
from services.registry.app.society.config import SocietySettings, reset_settings_cache
from services.registry.app.society.events import emit_event, utcnow
from services.registry.app.society.intents import FORBIDDEN_INTENT_TYPES, AgentDecision, IntentType, validate_intents
from services.registry.app.society.policy import check_run_budget, evaluate_intent, risk_of
from services.registry.app.society.runs import dispatch_pending_events
from services.registry.app.society.seed import seed_society

REPO = pathlib.Path(__file__).resolve().parent.parent.parent


def _validated(type_name, payload):
    d = AgentDecision(decision_summary="t", intents=[{"type": type_name, "payload": payload}])
    return validate_intents(d, uuid.uuid4())[0]


def _settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    reset_settings_cache()
    return SocietySettings()


@pytest.fixture
def fleet(db, society_settings):
    report = seed_society(db)
    agents = {}
    grants = {}
    from services.registry.app.models import Agent

    for role, aid in report.agents.items():
        agents[role] = db.query(Agent).filter(Agent.id == aid).first()
        grants[role] = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == aid).first()
    return agents, grants


@pytest.mark.parametrize("ftype", sorted(t.value for t in FORBIDDEN_INTENT_TYPES))
def test_forbidden_types_are_high_risk_and_always_denied(fleet, society_settings, ftype):
    agents, grants = fleet
    v = _validated(ftype, {"anything": "goes"})
    assert risk_of(v.intent_type) == IntentRiskClass.HIGH
    for role in agents:
        verdict = evaluate_intent(v, grant=grants[role], settings=society_settings, agent=agents[role])
        assert verdict.decision == PolicyDecision.DENY, (role, ftype, verdict.reason)
        assert "fail closed" in verdict.reason


def test_unknown_type_is_invalid(fleet, society_settings):
    agents, grants = fleet
    v = _validated("MAKE_ME_ADMIN", {})
    verdict = evaluate_intent(v, grant=grants["scout"], settings=society_settings, agent=agents["scout"])
    assert verdict.decision == PolicyDecision.INVALID


def test_type_not_in_grant_is_denied(fleet, society_settings):
    agents, grants = fleet
    v = _validated("REQUEST_CODE_CHANGE", {"title": "t", "spec": {"description": "d", "files_allowed": ["docs/x.md"], "acceptance_tests": ["tests/society/acceptance/test_candidate_docs.py"]}})
    verdict = evaluate_intent(v, grant=grants["scout"], settings=society_settings, agent=agents["scout"])
    assert verdict.decision == PolicyDecision.DENY and "allowed_intents" in verdict.reason
    verdict = evaluate_intent(v, grant=grants["architect"], settings=society_settings, agent=agents["architect"])
    assert verdict.decision == PolicyDecision.ALLOW


def test_risk_ceiling_blocks_medium_for_low_grant(fleet, society_settings):
    agents, grants = fleet
    g = grants["scout"]
    g.allowed_intents = list(g.allowed_intents) + ["CREATE_TASK"]  # operator mistake: allowed but ceiling LOW
    v = _validated("CREATE_TASK", {"callee_agent": "Society_Builder", "capability": "implement_change", "input": {"candidate_id": "x"}, "max_budget": 5})
    verdict = evaluate_intent(v, grant=g, settings=society_settings, agent=agents["scout"])
    assert verdict.decision == PolicyDecision.DENY and "ceiling" in verdict.reason


def test_code_intents_denied_when_flag_off(fleet, monkeypatch):
    agents, grants = fleet
    settings = _settings(monkeypatch, SOCIETY_AUTONOMOUS_CODE_ENABLED="false")
    v = _validated("REQUEST_CODE_CHANGE", {"title": "t", "spec": {"description": "d", "files_allowed": ["docs/x.md"], "acceptance_tests": ["tests/x.py"]}})
    verdict = evaluate_intent(v, grant=grants["architect"], settings=settings, agent=agents["architect"])
    assert verdict.decision == PolicyDecision.DENY and "SOCIETY_AUTONOMOUS_CODE_ENABLED" in verdict.reason


def test_staging_and_production_deploy_gates(fleet, monkeypatch):
    agents, grants = fleet
    g = grants["architect"]
    g.allowed_intents = list(g.allowed_intents) + ["REQUEST_STAGING_DEPLOY", "REQUEST_PRODUCTION_DEPLOY"]
    settings = _settings(monkeypatch, SOCIETY_STAGING_DEPLOY_ENABLED="false", SOCIETY_PRODUCTION_DEPLOY_ENABLED="true")
    assert settings.production_deploy_enabled is False, "production deploy must be hard OFF even when env says true"
    v = _validated("REQUEST_STAGING_DEPLOY", {"candidate_id": str(uuid.uuid4())})
    assert evaluate_intent(v, grant=g, settings=settings, agent=agents["architect"]).decision == PolicyDecision.DENY
    v = _validated("REQUEST_PRODUCTION_DEPLOY", {"candidate_id": str(uuid.uuid4())})
    assert evaluate_intent(v, grant=g, settings=settings, agent=agents["architect"]).decision == PolicyDecision.DENY
    settings = _settings(monkeypatch, SOCIETY_STAGING_DEPLOY_ENABLED="true")
    g.risk_ceiling = IntentRiskClass.MEDIUM
    v = _validated("REQUEST_STAGING_DEPLOY", {"candidate_id": str(uuid.uuid4())})
    assert evaluate_intent(v, grant=g, settings=settings, agent=agents["architect"]).decision == PolicyDecision.ALLOW


def test_escrow_cap_is_min_of_grant_and_global(fleet, monkeypatch):
    agents, grants = fleet
    g = grants["architect"]  # max_task_escrow_credits=50
    settings = _settings(monkeypatch, SOCIETY_MAX_TASK_ESCROW_CREDITS="20")
    base = {"callee_agent": "Society_Builder", "capability": "implement_change", "input": {"candidate_id": "x"}}
    assert evaluate_intent(_validated("CREATE_TASK", {**base, "max_budget": 20}), grant=g, settings=settings, agent=agents["architect"]).decision == PolicyDecision.ALLOW
    v = evaluate_intent(_validated("CREATE_TASK", {**base, "max_budget": 21}), grant=g, settings=settings, agent=agents["architect"])
    assert v.decision == PolicyDecision.DENY and "escrow cap" in v.reason
    # self-task denied
    v = evaluate_intent(_validated("CREATE_TASK", {**base, "callee_agent": "Society_Architect", "max_budget": 5}), grant=g, settings=settings, agent=agents["architect"])
    assert v.decision == PolicyDecision.DENY and "itself" in v.reason


def test_scope_checks_memory_goal_message(fleet, society_settings):
    agents, grants = fleet
    b = grants["builder"]  # memory_scopes = ["agent"]
    v = _validated("WRITE_MEMORY", {"title": "t", "content": "c", "scope": "society"})
    assert evaluate_intent(v, grant=b, settings=society_settings, agent=agents["builder"]).decision == PolicyDecision.DENY
    v = _validated("WRITE_MEMORY", {"title": "t", "content": "c", "scope": "agent"})
    assert evaluate_intent(v, grant=b, settings=society_settings, agent=agents["builder"]).decision == PolicyDecision.ALLOW
    s = grants["scout"]  # goal_owners = ["agent"]
    v = _validated("CREATE_GOAL", {"title": "t", "owner": "society"})
    assert evaluate_intent(v, grant=s, settings=society_settings, agent=agents["scout"]).decision == PolicyDecision.DENY
    v = _validated("SEND_MESSAGE", {"to_agent": "Society_Scout", "title": "t", "content": "c"})
    assert evaluate_intent(v, grant=s, settings=society_settings, agent=agents["scout"]).decision == PolicyDecision.DENY
    s.resource_scopes = {**(s.resource_scopes or {}), "message_targets": ["Society_Governor"]}
    v = _validated("SEND_MESSAGE", {"to_agent": "Society_Architect", "title": "t", "content": "c"})
    assert evaluate_intent(v, grant=s, settings=society_settings, agent=agents["scout"]).decision == PolicyDecision.DENY
    v = _validated("SEND_MESSAGE", {"to_agent": "Society_Governor", "title": "t", "content": "c"})
    assert evaluate_intent(v, grant=s, settings=society_settings, agent=agents["scout"]).decision == PolicyDecision.ALLOW


def test_approval_required_and_disabled_and_paused_grants(fleet, society_settings):
    agents, grants = fleet
    g = grants["scout"]
    g.approval_required_intents = ["CREATE_IMPROVEMENT"]
    v = _validated("CREATE_IMPROVEMENT", {"title": "t", "problem": "p", "proposed_change": "c"})
    assert evaluate_intent(v, grant=g, settings=society_settings, agent=agents["scout"]).decision == PolicyDecision.APPROVAL_REQUIRED
    g.paused_until = utcnow() + timedelta(minutes=5)
    assert evaluate_intent(v, grant=g, settings=society_settings, agent=agents["scout"]).decision == PolicyDecision.DENY
    g.paused_until = None
    g.enabled = False
    assert evaluate_intent(v, grant=g, settings=society_settings, agent=agents["scout"]).decision == PolicyDecision.DENY
    assert evaluate_intent(v, grant=None, settings=society_settings, agent=agents["scout"]).decision == PolicyDecision.DENY


def test_run_budget_gate_runs_per_hour(fleet, society_settings, db):
    agents, grants = fleet
    g = grants["scout"]
    g.max_runs_per_hour = 1
    g.wake_cooldown_seconds = 0
    db.commit()
    ev = emit_event(db, event_type="t")
    db.commit()
    dispatch_pending_events(db, settings=society_settings, routing={"t": ["scout"]})
    run = db.query(AgentRun).first()
    assert check_run_budget(db, agent=agents["scout"], grant=g, settings=society_settings, run=run).ok
    run.status = "completed"
    run.started_at = utcnow()
    db.commit()
    ev2 = emit_event(db, event_type="t")
    db.commit()
    dispatch_pending_events(db, settings=society_settings, routing={"t": ["scout"]})
    run2 = db.query(AgentRun).filter(AgentRun.event_id == ev2.id).first()
    verdict = check_run_budget(db, agent=agents["scout"], grant=g, settings=society_settings, run=run2)
    assert not verdict.ok and "runs/hour" in verdict.reason
    assert ev.id != ev2.id


def test_no_executor_mutates_grants_wallets_or_secrets():
    """Structural invariant: the executor never writes grants, wallet
    balances/caps, users or secrets. Checked on source to survive refactors."""
    src = inspect.getsource(executor_module)
    for forbidden in (
        "AgentCapabilityGrant(",
        "grant.allowed_intents",
        "grant.risk_ceiling",
        "grant.daily_model_budget_usd",
        "grant.max_task_escrow_credits",
        "balance_credits",
        "balance_usdc",
        "spending_cap",
        "password_hash",
        "public_key",
        "ScopedToken",
        "OrchestratorPartner",
        "subprocess",
        "os.system",
    ):
        assert forbidden not in src, f"executor references {forbidden!r}"
    assert not re.search(r"\bgrant\.\w+\s*=(?!=)", src), "executor assigns to a grant attribute"
    assert not re.search(r"\bwallet\.\w+\s*=(?!=)", src), "executor assigns to a wallet attribute"
    # only the seed module constructs grants
    society_dir = REPO / "services" / "registry" / "app" / "society"
    writers = [p.name for p in society_dir.rglob("*.py") if "AgentCapabilityGrant(" in p.read_text(encoding="utf-8")]
    assert writers == ["seed.py"], writers


def test_forbidden_types_have_no_executor_handler():
    for t in FORBIDDEN_INTENT_TYPES:
        assert t not in executor_module.HANDLERS
    assert set(executor_module.HANDLERS) == {t for t in IntentType if t not in FORBIDDEN_INTENT_TYPES}


def test_daily_budget_decimal_math(fleet, society_settings, db):
    agents, grants = fleet
    g = grants["scout"]
    g.daily_model_budget_usd = Decimal("0.001")
    g.wake_cooldown_seconds = 0
    db.commit()
    emit_event(db, event_type="t")
    db.commit()
    dispatch_pending_events(db, settings=society_settings, routing={"t": ["scout"]})
    run = db.query(AgentRun).first()
    run.cost_usd = Decimal("0.001")
    run.status = "completed"
    run.started_at = utcnow() - timedelta(hours=2)
    db.commit()
    ev2 = emit_event(db, event_type="t")
    db.commit()
    dispatch_pending_events(db, settings=society_settings, routing={"t": ["scout"]})
    run2 = db.query(AgentRun).filter(AgentRun.event_id == ev2.id).first()
    verdict = check_run_budget(db, agent=agents["scout"], grant=g, settings=society_settings, run=run2)
    assert not verdict.ok and "agent daily model budget" in verdict.reason
