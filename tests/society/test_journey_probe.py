"""The hourly core-journey probe: O1's metric source, through the ordinary REST API (no model).

Money invariant: the probe's capability is free, so the escrow it locks and releases is 0
credits; it never writes a wallet or a transaction itself, it only reads the settlement.
"""

from __future__ import annotations

import dataclasses

from sqlalchemy import text

from services.registry.app.maintenance import journey_probe
from services.registry.app.maintenance.config import MaintenanceSettings
from services.registry.app.society import metrics
from services.registry.app.society.config import SocietySettings

from .conftest import auth

EMAIL = "core-journey-probe@agentnet-staging.dev"


def _http(api_client):
    def call(method, path, body, token):
        r = api_client.request(method, path, json=body, headers=auth(token) if token else {})
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}
    return call


def _journey(db, api_client):
    return journey_probe.run(_http(api_client), email=EMAIL, secret="s3cret", verify=lambda e: journey_probe.mark_verified(db, e))


def test_the_journey_runs_through_the_api_settles_and_moves_no_money(db, api_client):
    res = journey_probe.observe(db, MaintenanceSettings(), _journey(db, api_client), target="staging")
    assert res.ok and res.step == "settled", res
    task = db.execute(text("SELECT status, escrow_amount, caller_agent_id, callee_agent_id FROM task_sessions WHERE id = :t"), {"t": res.task_id}).mappings().one()
    assert task["status"] == "completed" and task["escrow_amount"] == 0
    tx = db.execute(text("SELECT status, amount FROM transactions WHERE task_session_id = :t"), {"t": res.task_id}).mappings().one()
    assert tx["status"] == "completed" and float(tx["amount"]) == 0, "settled through the escrow path, 0 credits"
    wallets = db.execute(text("SELECT balance_credits, reserved_credits FROM wallets WHERE owner_type = 'agent' AND owner_id IN (:a, :b)"),
                         {"a": task["caller_agent_id"], "b": task["callee_agent_id"]}).all()
    assert len(wallets) == 2 and all(tuple(w) == (0, 0) for w in wallets), "no money moved, nothing left reserved"
    obs = db.execute(text("SELECT ok, payload FROM maintenance_observations WHERE sli = 'core_journey'")).mappings().one()
    assert obs["ok"] is True and obs["payload"]["step"] == "settled" and obs["payload"]["duration_s"] >= 0
    assert metrics.read(db, "core_journey_success_rate") == 1.0 and metrics.read(db, "core_journey_p95_minutes") is not None

    again = journey_probe.observe(db, MaintenanceSettings(), _journey(db, api_client), target="staging")
    assert again.ok and again.task_id != res.task_id
    assert db.execute(text("SELECT COUNT(*) FROM agents WHERE name LIKE 'Core_Journey_%'")).scalar() == 2, "the probe reuses its two agents"


def test_a_failed_journey_opens_a_core_journey_incident_and_a_settled_one_heals_it(db, api_client):
    def down(method, path, body, token):
        return 503, {}
    bad = journey_probe.observe(db, MaintenanceSettings(), journey_probe.run(down, email=EMAIL, secret="s", verify=lambda e: None), target="staging")
    assert not bad.ok and bad.step == "register"
    inc = db.execute(text("SELECT desired_state_ref, incident_class, priority, status FROM maintenance_incidents")).mappings().one()
    assert dict(inc) == {"desired_state_ref": "core_journey:escrow", "incident_class": "FUNCTIONAL_CONTRACT", "priority": "P1", "status": "open"}
    assert metrics.read(db, "core_journey_success_rate") == 0.0
    unsettled = journey_probe.observe(db, MaintenanceSettings(), journey_probe.JourneyResult(True, 1.5, "settled"), target="staging")
    assert not unsettled.ok and unsettled.step == "settlement", "a claimed success without a settled escrow transaction is a failure"
    assert journey_probe.observe(db, MaintenanceSettings(), _journey(db, api_client), target="staging").ok
    assert db.execute(text("SELECT healthy_streak FROM maintenance_incidents")).scalar() == 1, "a settled journey grows the recovery streak"
    assert metrics.read(db, "core_journey_success_rate") == round(1 / 3, 4)


def test_the_probe_is_refused_outside_staging_or_without_its_own_origin_and_secret(monkeypatch):
    s = dataclasses.replace(SocietySettings(), core_journey_probe_enabled=True, core_journey_probe_api_origin="https://registry-staging.example")
    monkeypatch.setenv("CORE_JOURNEY_PROBE_SECRET", "x")
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert journey_probe.refusal(s) == "the core-journey probe runs on staging only"
    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert journey_probe.refusal(s) is None
    assert "required" in journey_probe.refusal(dataclasses.replace(s, core_journey_probe_api_origin=""))
    assert SocietySettings().core_journey_probe_enabled is False, "off unless enabled"
