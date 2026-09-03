"""Public/operator split, operator authorization, and world-event ingress."""

from __future__ import annotations

import asyncio
import uuid

from services.registry.app.models import SocietyEvent
from services.registry.app.society.cognition import ScriptedRoleModel
from services.registry.app.society.config import SocietySettings, reset_settings_cache
from services.registry.app.society.events import EventType, emit_event
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker

from .conftest import auth

OPERATOR_ONLY = [
    "/v1/society/config",
    "/v1/society/events",
    "/v1/society/runs",
    "/v1/society/intents",
    "/v1/society/budget",
    "/v1/society/approvals",
    "/v1/society/operators",
    "/v1/society/ask?q=goals",
]
PUBLIC = ["/v1/society/status", "/v1/society/metrics", "/v1/society/candidates"]

PRIVATE_MARKERS = ("payload", "context_summary", "decision_summary", "content", "memory", "wallet", "balance", "api_key", "workspace_path", "repo_root", "model_base_url", "title", "error", "policy_reason", "spec", "qa_report")


def _story(db, SessionLocal, settings, grants_with_no_cooldown):
    seed_society(db)
    grants_with_no_cooldown()
    corr = uuid.uuid4()
    emit_event(db, event_type=EventType.PLATFORM_METRIC_ANOMALY, payload={"metric": "PRIVATE-TELEMETRY-INPUT-42", "description": "private details here", "severity_score": 65}, correlation_id=corr)
    db.commit()
    worker = SocietyWorker(SessionLocal, settings=settings, model=ScriptedRoleModel(), worker_id="api")
    asyncio.run(worker.run_until_idle(max_cycles=40))
    return corr


def test_operator_surfaces_require_operator_role(api_client, db, society_settings, user_token, agent_token):
    _, user_tok = user_token(None)
    _, producer_tok = user_token("event_producer")
    _, agent_tok = agent_token()
    for path in OPERATOR_ONLY:
        assert api_client.get(path).status_code == 401, path
        assert api_client.get(path, headers=auth(user_tok)).status_code == 403, path
        assert api_client.get(path, headers=auth(producer_tok)).status_code == 403, path
        assert api_client.get(path, headers=auth(agent_tok)).status_code == 403, path
    _, op_tok = user_token("operator")
    for path in OPERATOR_ONLY:
        assert api_client.get(path, headers=auth(op_tok)).status_code == 200, path


def test_scoped_tokens_never_confer_operator(api_client, db, society_settings, user_token, make_agent):
    """A scoped spt_ token resolves to the agent owner in get_current_user;
    even if that owner is an operator, operator surfaces must refuse it."""
    import hashlib

    from services.registry.app.models import ScopedToken

    owner, _ = user_token("operator")
    agent = make_agent("Owned_Agent", user=owner)
    raw = "spt_" + uuid.uuid4().hex
    db.add(ScopedToken(id=uuid.uuid4(), token_hash=hashlib.sha256(raw.encode()).hexdigest(), agent_id=agent.id, resource_type="domain", allowed_actions=["read"]))
    db.commit()
    r = api_client.get("/v1/society/config", headers=auth(raw))
    assert r.status_code == 403 and "user session token" in r.text
    r = api_client.post("/v1/society/events", headers=auth(raw), json={"event_type": "platform.metric.anomaly", "payload": {}})
    assert r.status_code == 403


def test_bootstrap_allowlist_grants_operator_centrally(api_client, db, society_settings, user_token, monkeypatch):
    user, tok = user_token(None, email="boot@ops.test")
    assert api_client.get("/v1/society/config", headers=auth(tok)).status_code == 403
    monkeypatch.setenv("SOCIETY_OPERATOR_BOOTSTRAP_EMAILS", "Boot@ops.test, other@ops.test")
    assert api_client.get("/v1/society/config", headers=auth(tok)).status_code == 200
    ops = api_client.get("/v1/society/operators", headers=auth(tok)).json()
    assert any(o["email"] == "boot@ops.test" and o.get("source") == "bootstrap_env" for o in ops)


def test_operator_can_assign_durable_roles_but_users_cannot(api_client, db, society_settings, user_token):
    op, op_tok = user_token("operator")
    target, target_tok = user_token(None, email="new@ops.test")
    assert api_client.post("/v1/society/operators", headers=auth(target_tok), json={"email": "new@ops.test", "role": "operator"}).status_code == 403
    r = api_client.post("/v1/society/operators", headers=auth(op_tok), json={"email": "new@ops.test", "role": "event_producer"})
    assert r.status_code == 200 and r.json()["role"] == "event_producer"
    db.refresh(target)
    assert target.society_role == "event_producer"
    assert api_client.post("/v1/society/events", headers=auth(target_tok), json={"event_type": "platform.metric.anomaly", "payload": {"metric": "x"}}).status_code == 201
    assert api_client.get("/v1/society/runs", headers=auth(target_tok)).status_code == 403  # producer is not operator
    r = api_client.post("/v1/society/operators", headers=auth(op_tok), json={"email": "new@ops.test", "role": None})
    assert r.status_code == 200 and r.json()["role"] is None
    assert api_client.post("/v1/society/operators", headers=auth(op_tok), json={"email": "nobody@ops.test", "role": "operator"}).status_code == 404
    assert api_client.post("/v1/society/operators", headers=auth(op_tok), json={"email": "new@ops.test", "role": "root"}).status_code == 422


def test_public_surfaces_are_sanitised(api_client, db, SessionLocal, society_settings, grants_with_no_cooldown):
    corr = _story(db, SessionLocal, society_settings, grants_with_no_cooldown)
    for path in PUBLIC + [f"/v1/society/story/{corr}"]:
        r = api_client.get(path)
        assert r.status_code == 200, path
        body = r.text
        assert "PRIVATE-TELEMETRY-INPUT-42" not in body and "private details" not in body, path
        assert "Improve:" not in body, path  # proposal/candidate titles derive from private input
        for marker in PRIVATE_MARKERS:
            assert f'"{marker}"' not in body, (path, marker)
    story = api_client.get(f"/v1/society/story/{corr}").json()
    assert {r["role"] for r in story["runs"]} >= {"scout", "governor", "architect", "builder", "qa"}
    assert all(set(i) == {"id", "seq", "intent_type", "risk_class", "policy_decision", "execution_status"} for r in story["runs"] for i in r["intents"])
    assert story["candidates"][0]["status"] == "ready" and "title" not in story["candidates"][0]
    status = api_client.get("/v1/society/status").json()
    assert status["production_deploy_enabled"] is False and "model_name" not in status and "fleet" in status
    assert all(set(f) == {"role", "enabled", "paused"} for f in status["fleet"])


def test_operator_detail_surfaces_expose_full_story(api_client, db, SessionLocal, society_settings, grants_with_no_cooldown, user_token):
    corr = _story(db, SessionLocal, society_settings, grants_with_no_cooldown)
    _, op_tok = user_token("operator")
    detail = api_client.get(f"/v1/society/story/{corr}/detail", headers=auth(op_tok)).json()
    assert detail["events"][0]["payload"]["metric"] == "PRIVATE-TELEMETRY-INPUT-42"
    assert detail["runs"][0]["decision_summary"]
    runs = api_client.get("/v1/society/runs", headers=auth(op_tok), params={"correlation_id": str(corr)}).json()
    one = api_client.get(f"/v1/society/runs/{runs[0]['id']}", headers=auth(op_tok)).json()
    assert one["context_summary"] and one["intents"] and one["model_requests"] >= 0
    cands = api_client.get("/v1/society/candidates").json()
    full = api_client.get(f"/v1/society/candidates/{cands[0]['id']}", headers=auth(op_tok)).json()
    assert full["qa_report"]["checks"] and full["title"]
    assert api_client.get(f"/v1/society/candidates/{cands[0]['id']}").status_code == 401
    budget = api_client.get("/v1/society/budget", headers=auth(op_tok)).json()
    assert "wallets" in budget and "model_spend_today_usd" in budget
    for q, key in [("active goals?", "goals"), ("who is working", "working"), ("what happened in the last story", "recent"), ("pending proposals", "proposals"), ("intents awaiting approval", "blocked"), ("which candidate awaits qa", "candidates"), ("model budget used", "budget"), ("why was an intent denied", "why_denied")]:
        body = api_client.get("/v1/society/ask", headers=auth(op_tok), params={"q": q}).json()
        assert key in body["answers"], (q, body)


def test_ingress_allowlist_reserved_types_and_payload_limits(api_client, db, society_settings, user_token):
    _, tok = user_token("event_producer")
    post = lambda body: api_client.post("/v1/society/events", headers=auth(tok), json=body)  # noqa: E731
    assert post({"event_type": "proposal.approved", "payload": {}}).status_code == 400
    assert post({"event_type": "intent.approved", "payload": {}}).status_code == 400
    assert post({"event_type": "agent.message.received", "payload": {}}).status_code == 400
    assert post({"event_type": "society.heartbeat", "payload": {}}).status_code == 400
    assert post({"event_type": "some.random.thing", "payload": {}}).status_code == 400
    assert post({"event_type": "platform.metric.anomaly", "payload": {"target_agent_id": str(uuid.uuid4())}}).status_code == 422
    assert post({"event_type": "platform.metric.anomaly", "payload": {"blob": "x" * 5000}}).status_code == 422
    deep = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
    assert post({"event_type": "platform.metric.anomaly", "payload": deep}).status_code == 422
    assert post({"event_type": "platform.metric.anomaly", "payload": {"items": list(range(100))}}).status_code == 422
    big = {f"k{i}": "v" * 1500 for i in range(10)}
    assert post({"event_type": "platform.metric.anomaly", "payload": big}).status_code == 413
    r = post({"event_type": "platform.health.degraded", "payload": {"component": "registry", "detail": "p99 latency 4s"}})
    assert r.status_code == 201 and r.json()["actor_type"] == "user" and r.json()["duplicate"] is False


def test_ingress_idempotency_and_rate_limits(api_client, db, society_settings, user_token, monkeypatch):
    monkeypatch.setenv("SOCIETY_INGRESS_MAX_PER_ACTOR_PER_HOUR", "3")
    monkeypatch.setenv("SOCIETY_INGRESS_MAX_PER_HOUR", "5")
    reset_settings_cache()
    _, a_tok = user_token("event_producer", email="a@prod.test")
    _, b_tok = user_token("event_producer", email="b@prod.test")
    post = lambda tok, body: api_client.post("/v1/society/events", headers=auth(tok), json=body)  # noqa: E731
    r1 = post(a_tok, {"event_type": "platform.metric.anomaly", "payload": {"metric": "x"}, "idempotency_key": "webhook-1"})
    r2 = post(a_tok, {"event_type": "platform.metric.anomaly", "payload": {"metric": "x"}, "idempotency_key": "webhook-1"})
    assert r1.status_code == 201 and r2.status_code == 201 and r2.json()["id"] == r1.json()["id"] and r2.json()["duplicate"] is True
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == "platform.metric.anomaly").count() == 1
    assert post(a_tok, {"event_type": "platform.metric.anomaly", "payload": {"n": 2}}).status_code == 201
    assert post(a_tok, {"event_type": "platform.metric.anomaly", "payload": {"n": 3}}).status_code == 201
    assert post(a_tok, {"event_type": "platform.metric.anomaly", "payload": {"n": 4}}).status_code == 429  # per-actor
    # redelivery of an existing key still works even when rate-limited
    assert post(a_tok, {"event_type": "platform.metric.anomaly", "payload": {"metric": "x"}, "idempotency_key": "webhook-1"}).status_code == 201
    assert post(b_tok, {"event_type": "platform.metric.anomaly", "payload": {"n": 5}}).status_code == 201
    assert post(b_tok, {"event_type": "platform.metric.anomaly", "payload": {"n": 6}}).status_code == 201
    assert post(b_tok, {"event_type": "platform.metric.anomaly", "payload": {"n": 7}}).status_code == 429  # global (5)
    reset_settings_cache()
    assert SocietySettings().ingress_max_events_per_hour >= 1


def test_duplicate_ingress_never_creates_duplicate_story(api_client, db, SessionLocal, society_settings, grants_with_no_cooldown, user_token):
    seed_society(db)
    grants_with_no_cooldown()
    _, tok = user_token("event_producer")
    for _ in range(3):
        api_client.post("/v1/society/events", headers=auth(tok), json={"event_type": "platform.metric.anomaly", "payload": {"metric": "dup", "severity_score": 60}, "idempotency_key": "dup-story"})
    worker = SocietyWorker(SessionLocal, settings=society_settings, model=ScriptedRoleModel(), worker_id="w")
    asyncio.run(worker.run_until_idle(max_cycles=40))
    from services.registry.app.models import AgentRun, ImprovementProposal

    assert db.query(ImprovementProposal).count() == 1
    assert db.query(AgentRun).filter(AgentRun.role == "scout", AgentRun.attempt == 1).count() >= 1
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == "platform.metric.anomaly").count() == 1
