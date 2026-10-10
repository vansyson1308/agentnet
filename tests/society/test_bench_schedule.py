"""The daily scheduled bench: once per UTC day after the hour, off by default, no credential beyond model + DB."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from services.registry.app.models import AgentRun, SocietyEvent
from services.registry.app.society import bench_schedule
from services.registry.app.society.events import EventType


def _on(settings, **kw):
    return dataclasses.replace(settings, bench_daily_enabled=True, github_repository="owner/repo", bench_daily_hour_utc=3, **kw)


def test_the_daily_bench_starts_once_per_utc_day_after_its_hour_and_calls_no_model(db, society_settings):
    started = []
    s = _on(society_settings)
    assert bench_schedule.maybe_start_daily_bench(db, society_settings, now=datetime(2026, 10, 10, 9, tzinfo=timezone.utc), spawn=started.append) is False, "off by default"
    assert bench_schedule.maybe_start_daily_bench(db, s, now=datetime(2026, 10, 10, 2, tzinfo=timezone.utc), spawn=started.append) is False, "before the hour"
    for hour in (3, 4, 23):
        bench_schedule.maybe_start_daily_bench(db, s, now=datetime(2026, 10, 10, hour, tzinfo=timezone.utc), spawn=started.append)
    assert len(started) == 1
    assert bench_schedule.maybe_start_daily_bench(db, s, now=datetime(2026, 10, 11, 3, tzinfo=timezone.utc), spawn=started.append) is True and len(started) == 2
    evs = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.BENCH_DAILY_STARTED).all()
    assert [e.payload["day"] for e in evs] == ["2026-10-10", "2026-10-11"] and db.query(AgentRun).count() == 0


def test_the_bench_child_gets_model_and_database_settings_never_other_credentials(society_settings):
    environ = {"PATH": "/usr/bin", "SOCIETY_MODEL_API_KEY": "m", "SOCIETY_MODEL_NAME": "n", "POSTGRES_PASSWORD": "p", "POSTGRES_HOST": "h",
               "SOCIETY_GITHUB_TOKEN": "x", "MAINTENANCE_ATTESTATION_KEY": "x", "RELEASE_RAILWAY_TOKEN": "x", "JWT_SECRET_KEY": "x",
               "BENCH_HARNESS_REF": "candidate", "MAINTENANCE_BUILDER_MAX_TURNS": "10"}
    env = bench_schedule.child_env(_on(society_settings, bench_daily_samples=3, bench_daily_repeat=3), environ)
    assert {"SOCIETY_MODEL_API_KEY", "POSTGRES_PASSWORD", "POSTGRES_HOST", "PATH", "MAINTENANCE_BUILDER_MAX_TURNS"} <= set(env)
    assert not {"SOCIETY_GITHUB_TOKEN", "MAINTENANCE_ATTESTATION_KEY", "RELEASE_RAILWAY_TOKEN", "JWT_SECRET_KEY", "BENCH_HARNESS_REF"} & set(env)
    assert env["MAINTENANCE_BUILDER_SAMPLES"] == "3" and env["BENCH_REPEAT"] == "3" and env["BENCH_SPLIT"] == "all"
