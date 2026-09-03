"""/v1/society/* smoke: public status + operator story after a scripted loop
(auth split is covered in test_operator_api.py, approvals in test_approvals.py)."""

from __future__ import annotations

import asyncio
import uuid

from services.registry.app.society.cognition import ScriptedRoleModel
from services.registry.app.society.events import EventType, emit_event
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker

from .conftest import auth


def test_status_and_story_after_a_loop(api_client, db, SessionLocal, society_settings, grants_with_no_cooldown, user_token):
    seed_society(db)
    grants_with_no_cooldown()
    r = api_client.get("/v1/society/status")
    assert r.status_code == 200 and r.json()["production_deploy_enabled"] is False and r.json()["fleet_size"] == 6
    corr = uuid.uuid4()
    ev = emit_event(db, event_type=EventType.PLATFORM_METRIC_ANOMALY, payload={"metric": "latency_p99", "severity_score": 65}, correlation_id=corr)
    db.commit()
    asyncio.run(SocietyWorker(SessionLocal, settings=society_settings, model=ScriptedRoleModel(), worker_id="api").run_until_idle(max_cycles=40))
    story = api_client.get(f"/v1/society/story/{corr}").json()
    assert story["events"][0]["id"] == str(ev.id) and story["candidates"][0]["status"] == "ready"
    _, op_tok = user_token("operator")
    detail = api_client.get(f"/v1/society/story/{corr}/detail", headers=auth(op_tok)).json()
    assert detail["runs"][0]["decision_summary"] and detail["events"][0]["payload"]["metric"] == "latency_p99"
    metrics = api_client.get("/v1/society/metrics").json()
    assert metrics["runs"].get("completed", 0) >= 5 and "model_cost_usd" not in metrics
