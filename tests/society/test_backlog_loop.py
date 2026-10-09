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
    assert [(i["source"], i.get("task_id") or i.get("issue_number"), i["failure_class"]) for i in items] == [("bench", "hard-task", "turn_budget"), ("github_issue", 7, "agent_ok_issue")]
    assert emit_heartbeat(db, dataclasses.replace(society_settings, heartbeat_interval_seconds=3600), provider=_Issues()) is True
    evs = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.BACKLOG_ITEM).all()
    assert len(evs) == 2 and "secret-holdout" not in json.dumps([e.payload for e in evs])
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
    assert loop["backlog"] == 1 and loop["prs_merged_7d"] == 0 and loop["usd_per_merged_pr_7d"] is None
    assert "holdout" not in json.dumps(loop) and "secret-holdout" not in json.dumps(loop)
