"""Staging 2026-10-10/11: 37 society runs went DEAD ("worker error: IndexError: 4") for ~20h
and nothing alerted. >= 3 consecutive DEAD runs of a role now open a CONTROL_PLANE
maintenance incident and a public status flag; the circuit breaker and the incident both
recover on their own once the role completes runs again (runs._run_health)."""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
from datetime import timedelta

from sqlalchemy import text

from services.registry.app.maintenance.config import get_maintenance_settings
from services.registry.app.models import AgentCapabilityGrant
from services.registry.app.society.events import emit_event, utcnow
from services.registry.app.society.policy import check_run_budget
from services.registry.app.society.runs import claim_next_run, complete_run, dispatch_pending_events, fail_run
from services.registry.app.society.seed import seed_society

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _scout_run(db, settings, i):
    emit_event(db, event_type="t.health", payload={"i": i})
    db.commit()
    dispatch_pending_events(db, settings=settings, routing={"t.health": ["scout"]})
    return claim_next_run(db, worker_id="w", lease_seconds=60)


def _incident(db):
    return db.execute(text("SELECT incident_class, priority, status, healthy_streak FROM maintenance_incidents WHERE desired_state_ref = 'society_runs:scout'")).mappings().all()


def test_three_dead_runs_in_a_row_alert_and_the_breaker_and_incident_recover_on_their_own(db, society_settings, api_client):
    report = seed_society(db)
    for i in range(3):
        assert _incident(db) == [], "two DEAD runs are a retry story, not an incident"
        assert fail_run(db, _scout_run(db, society_settings, i), "worker error: IndexError: 4", settings=society_settings, retryable=False).value == "dead"
    assert [dict(r) for r in _incident(db)] == [{"incident_class": "CONTROL_PLANE", "priority": "P1", "status": "open", "healthy_streak": 0}]
    obs = db.execute(text("SELECT payload FROM maintenance_observations WHERE sli = 'society_runs' AND ok IS FALSE")).scalar()
    assert {k: obs[k] for k in ("role", "consecutive_dead", "error_class")} == {"role": "scout", "consecutive_dead": 3, "error_class": "IndexError"}
    status = api_client.get("/v1/society/status").json()
    assert status["dead_streak_roles"] == ["scout"] and "IndexError" not in json.dumps(status), "public flag is structural"
    assert {f["role"]: (f["paused"], f["consecutive_dead"]) for f in status["fleet"]}["scout"] == (True, 3)

    # the breaker paused the role; once the pause expires the next run is allowed and a completed run resets it
    grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == report.agents["scout"]).one()
    grant.paused_until = utcnow() - timedelta(seconds=1)
    db.commit()
    for i in range(get_maintenance_settings().recovery_streak):
        run = _scout_run(db, society_settings, 10 + i)
        assert check_run_budget(db, agent=run.agent, grant=grant, settings=society_settings, run=run).ok
        complete_run(db, run)
    db.refresh(grant)
    assert grant.consecutive_failures == 0 and api_client.get("/v1/society/status").json()["dead_streak_roles"] == []
    assert _incident(db)[0]["status"] == "recovered", "no operator reset needed: completed runs heal it"


def test_every_registry_module_imports_from_the_image_layout(tmp_path):
    """scripts/ci/import_all.py (run inside the built image by check_images.sh), here on a copy
    laid out like the image's /app: app/ is the top-level package, no repository around it."""
    shutil.copytree(ROOT / "services/registry/app", tmp_path / "app", ignore=shutil.ignore_patterns("__pycache__"))
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "ENVIRONMENT": "development", "JAEGER_ENABLED": "false", "POSTGRES_PASSWORD": "smoke-only"}
    proc = subprocess.run([sys.executable, "-c", (ROOT / "scripts/ci/import_all.py").read_text()], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "IMPORT FAILED" not in proc.stdout and "registry modules imported:" in proc.stdout
