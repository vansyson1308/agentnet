"""/v1/society/* endpoints answer only from persisted state."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from services.registry.app.society.cognition import ScriptedRoleModel
from services.registry.app.society.events import EventType, emit_event
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker


@pytest.fixture
def client(db, SessionLocal, monkeypatch):
    from fastapi.testclient import TestClient

    from services.registry.app.database import get_db
    from services.registry.app.main import app

    def _override():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(app)  # no context manager: startup hooks (Redis) are not run
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_status_and_config_reflect_flags(client, society_settings, db):
    seed_society(db)
    r = client.get("/v1/society/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["production_deploy_enabled"] is False
    assert {f["role"] for f in body["fleet"]} == {"governor", "scout", "architect", "builder", "qa", "security"}
    assert body["pending_events"] == 0 and body["active_runs"] == 0
    assert client.get("/v1/society/config").status_code == 401  # paths + provider config need auth
    from services.registry.app.auth import get_current_user
    from services.registry.app.main import app
    from services.registry.app.models import User

    user = User(id=uuid.uuid4(), email="cfg@test", password_hash="x")
    db.add(user)
    db.commit()
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        cfg = client.get("/v1/society/config").json()
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert cfg["settings"]["model_api_key"] in ("", "***")
    assert "scout" in cfg["roles"]


def test_story_runs_intents_candidates_and_ask_after_a_loop(client, db, SessionLocal, society_settings, grants_with_no_cooldown):
    seed_society(db)
    grants_with_no_cooldown()
    corr = uuid.uuid4()
    ev = emit_event(db, event_type=EventType.PLATFORM_METRIC_ANOMALY, payload={"metric": "latency_p99", "severity_score": 65}, correlation_id=corr)
    db.commit()
    worker = SocietyWorker(SessionLocal, settings=society_settings, model=ScriptedRoleModel(), worker_id="api")
    asyncio.run(worker.run_until_idle(max_cycles=40))

    story = client.get(f"/v1/society/story/{corr}").json()
    assert story["events"][0]["id"] == str(ev.id)
    assert {r["role"] for r in story["runs"]} >= {"scout", "governor", "architect", "builder", "qa"}
    assert all(r["intents"] is not None for r in story["runs"])
    assert story["candidates"] and story["candidates"][0]["status"] == "ready"

    runs = client.get("/v1/society/runs", params={"correlation_id": str(corr)}).json()
    assert runs and runs[0]["agent_name"] == "Society_Scout"
    one = client.get(f"/v1/society/runs/{runs[0]['id']}").json()
    assert one["intents"] and one["context_digest"]
    assert "chain_of_thought" not in str(one).lower()

    intents = client.get("/v1/society/intents", params={"execution_status": "executed"}).json()
    assert any(i["intent_type"] == "EVALUATE_CODE_CANDIDATE" for i in intents)
    cands = client.get("/v1/society/candidates").json()
    assert cands[0]["qa"]["verdict"] == "pass"
    detail = client.get(f"/v1/society/candidates/{cands[0]['id']}").json()
    assert detail["qa_report"]["checks"]

    metrics = client.get("/v1/society/metrics").json()
    assert metrics["runs"].get("completed", 0) >= 5 and metrics["policy_denials"] >= 0
    for q, key in [("what are the active goals?", "goals"), ("which agents are working", "working"), ("what happened recently", "recent"), ("pending proposals", "proposals"), ("what is blocked", "blocked"), ("which candidate awaits qa", "candidates"), ("current spend and budget", "budget"), (f"why did run {runs[0]['id']} fail", "why_failed")]:
        body = client.get("/v1/society/ask", params={"q": q}).json()
        assert key in body["answers"], (q, body)
        assert body["source"] == "persisted_state"
    unknown = client.get("/v1/society/ask", params={"q": "hello there"}).json()
    assert "status" in unknown["answers"] and "note" in unknown


def test_event_injection_requires_auth_and_rejects_reserved_types(client, db, society_settings):
    r = client.post("/v1/society/events", json={"event_type": "platform.metric.anomaly", "payload": {}})
    assert r.status_code == 401
    from services.registry.app.auth import get_current_user
    from services.registry.app.main import app
    from services.registry.app.models import User

    user = User(id=uuid.uuid4(), email="op@test", password_hash="x")
    db.add(user)
    db.commit()
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        r = client.post("/v1/society/events", json={"event_type": "proposal.approved", "payload": {}})
        assert r.status_code == 400 and "reserved" in r.text
        r = client.post("/v1/society/events", json={"event_type": "platform.metric.anomaly", "payload": {"target_agent_id": str(uuid.uuid4())}})
        assert r.status_code == 400
        r = client.post("/v1/society/events", json={"event_type": "platform.metric.anomaly", "payload": {"metric": "x"}, "idempotency_key": "api-1"})
        assert r.status_code == 201 and r.json()["actor_type"] == "user"
        r2 = client.post("/v1/society/events", json={"event_type": "platform.metric.anomaly", "payload": {"metric": "x"}, "idempotency_key": "api-1"})
        assert r2.json()["id"] == r.json()["id"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)
