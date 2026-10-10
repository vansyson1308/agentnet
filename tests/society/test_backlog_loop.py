"""The loop feeds itself: bench reports -> dev backlog -> Scout; idle = no model call; status."""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from services.registry.app.models import AgentRun, SocietyEvent
from services.registry.app.society import backlog
from services.registry.app.society.events import EventType
from services.registry.app.society.seed import seed_society
from services.registry.app.society.world import emit_heartbeat

PER_TASK = {"ok-task": {"split": "dev", "delivered": 3, "runs": ["pass"] * 3},
            "hard-task": {"split": "dev", "delivered": 1, "runs": ["turn_budget", "pass", "turn_budget"]},
            "flaky-task": {"split": "dev", "delivered": 2, "runs": ["pass", "tests_fail", "pass"]},
            "secret-holdout": {"split": "holdout", "delivered": 0, "runs": ["wrong_file"] * 3}}


def _report(db, revision="r-running"):
    db.execute(text("INSERT INTO society_bench_reports (id, revision, judge_revision, path, model, repeat, summary, per_task) VALUES (:i,:r,:r,'maintenance','m',3,:s,:p)"),
               {"i": str(uuid.uuid4()), "r": revision, "s": json.dumps({"pass_at_1": 0.44, "pass_at_k": 0.67, "splits": {"dev": {"pass_at_1": 0.67}, "holdout": {"pass_at_1": 0.0}}}),
                "p": json.dumps(PER_TASK)})
    db.commit()


class _Issues:
    def list_agent_ok_issues(self):
        return [{"number": 7, "title": "Owner asks: add a CSV export", "updated_at": "2026-10-09T10:00:00Z"}]


def test_failing_dev_tasks_and_owner_issues_reach_the_scout_once_and_never_the_holdout(db, society_settings, monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "r-running")
    seed_society(db)
    _report(db)
    items = backlog.collect(db, society_settings, _Issues())
    assert [(i["source"], i.get("task_id") or i.get("issue_number"), i["failure_class"]) for i in items] == [
        ("bench", "flaky-task", "tests_fail"), ("bench", "hard-task", "turn_budget"), ("github_issue", 7, "agent_ok_issue")], "an active task failing even 1 of 3 runs is fuel"
    assert items[0]["gap"] == "builder_harness" and "services/registry/app/maintenance/harness.py" in items[0]["harness_paths"]
    assert emit_heartbeat(db, dataclasses.replace(society_settings, heartbeat_interval_seconds=3600), provider=_Issues()) is True
    evs = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.BACKLOG_ITEM).all()
    assert len(evs) == 3 and "secret-holdout" not in json.dumps([e.payload for e in evs])
    assert backlog.publish(db, society_settings, items) == 0, "published once per report / issue update"


def test_an_idle_heartbeat_emits_nothing_a_model_could_be_woken_by(db, society_settings, monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "r-running")
    seed_society(db)
    s = dataclasses.replace(society_settings, heartbeat_interval_seconds=3600)
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    assert emit_heartbeat(db, s, now=now) is False and emit_heartbeat(db, s, now=now) is False
    types = [e.event_type for e in db.query(SocietyEvent).all()]
    assert types == [EventType.SOCIETY_HEARTBEAT_IDLE] and db.query(AgentRun).count() == 0


def test_status_reports_the_loop_structurally(db, api_client, monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "r-running")
    _report(db)
    loop = api_client.get("/v1/society/status").json()["loop"]
    assert loop["bench"]["pass_at_1"] == 0.44 and loop["bench"]["dev"] == {"pass_at_1": 0.67} and loop["bench"]["stale"] is False
    assert loop["backlog"] == 2 and loop["prs_merged_7d"] == 0 and loop["usd_per_merged_pr_7d"] is None
    assert "holdout" not in json.dumps(loop) and "secret-holdout" not in json.dumps(loop)


def test_status_shows_the_last_seven_reports_as_a_trend(db, api_client, monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "r-running")
    for i in range(9):
        _report(db, revision=f"r-{i}")
    trend = api_client.get("/v1/society/status").json()["loop"]["trend"]
    assert len(trend) == 7 and trend[0]["revision"] == "r-8" and trend[-1]["revision"] == "r-2", "latest first"
    assert trend[0]["pass_at_1"] == 0.44 and trend[0]["dev_pass_at_1"] == 0.67 and trend[0]["candidate"] is False and "holdout" not in json.dumps(trend)


def _incident(db, *, case_state=None, resumable=False):
    from services.registry.app.maintenance.orm import MaintenanceIncident, RepairCase

    now = datetime.now(timezone.utc)
    inc = MaintenanceIncident(id=uuid.uuid4(), fingerprint=uuid.uuid4().hex, incident_class="UI_RENDERING", priority="P2", severity="major", source="surface_monitor",
                              target="staging", desired_state_ref="public_surface:/agents", first_observed_at=now, last_observed_at=now, current_evidence_digest="d" * 64,
                              case_count=1 if case_state else 0)
    db.add(inc)
    db.flush()
    if case_state:
        terminal = {"terminal_at": now, "terminal_reason": "owner_approval_required", "resumable": resumable} if case_state == "SAFELY_ESCALATED" else {}
        db.add(RepairCase(id=uuid.uuid4(), incident_id=inc.id, state=case_state, priority="P2", repair_class="code", case_deadline_at=now, next_action_at=now, deadline_at=now, **terminal))
    db.commit()
    return inc


def test_open_incidents_without_a_live_repair_case_are_fuel_and_covered_ones_are_not(db, society_settings, monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "r-running")
    uncovered, escalated = _incident(db), _incident(db, case_state="SAFELY_ESCALATED")
    covered, awaiting_owner = _incident(db, case_state="BUILDING"), _incident(db, case_state="SAFELY_ESCALATED", resumable=True)
    items = backlog.collect(db, society_settings)
    assert [i["incident_id"] for i in items if i["source"] == "maintenance_incident"] == [str(uncovered.id), str(escalated.id)]
    assert str(covered.id) not in json.dumps(items) and str(awaiting_owner.id) not in json.dumps(items) and all(set(i) == {"source", "key", "incident_id", "failure_class", "priority", "target", "cases_so_far"} for i in items)
