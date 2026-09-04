"""
Schema + migration invariants for the society runtime.

1. ``init-db/16-society-runtime.sql`` must be byte-identical to the DDL in
   ``app/society/schema_sql.py`` (which migration 0007 embeds). Two copies
   exist only because a fresh Postgres volume runs the SQL bundle while an
   existing DB runs alembic; drift between them is a deploy-time bug.
2. Migration 0007 is idempotent on top of the bundle.
3. REGRESSION: ``alembic upgrade head`` must PERSIST. Before this change
   ``migrations/env.py`` rolled back every migration (SQLAlchemy 2.x
   autobegin + an unconditional ``ROLLBACK`` in ``finally``), so
   ``alembic_version`` was never written and every container boot re-ran
   all revisions. We run alembic twice against a scratch DB and assert the
   second run applies nothing.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
REGISTRY = REPO / "services" / "registry"

sys.path.insert(0, str(REGISTRY))
from app.society.schema_sql import SOCIETY_RUNTIME_SQL  # noqa: E402

from .conftest import PG, TEST_DB, _admin_connection, _bootstrap_schema  # noqa: E402


def test_init_db_sql_matches_schema_module():
    on_disk = (REGISTRY / "init-db" / "16-society-runtime.sql").read_text(encoding="utf-8")
    assert on_disk.strip() == SOCIETY_RUNTIME_SQL.strip()


def test_migration_0007_embeds_schema_module_and_chains_from_0006():
    text = (REGISTRY / "migrations" / "versions" / "0007_society_runtime.py").read_text()
    assert 'revision = "0007_society_runtime"' in text
    assert 'down_revision = "0006_email_verified"' in text
    assert "SOCIETY_RUNTIME_SQL" in text


def test_every_create_statement_is_idempotent():
    for line in SOCIETY_RUNTIME_SQL.splitlines():
        if line.startswith("CREATE TABLE") or line.startswith("CREATE INDEX"):
            assert "IF NOT EXISTS" in line, line


def test_society_ddl_applies_twice(society_db_url, engine):
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text(SOCIETY_RUNTIME_SQL))  # already applied by bootstrap; must be a no-op
        n = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.tables WHERE table_name IN "
                "('society_events','agent_runs','agent_intents','agent_capability_grants','code_candidates')"
            )
        ).scalar()
    assert n == 5


def _alembic(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REGISTRY,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_alembic_upgrade_persists_and_is_idempotent(society_db_url):
    """Uses a separate scratch DB so the shared test DB is untouched."""
    scratch = TEST_DB + "_alembic"
    admin = _admin_connection()
    try:
        cur = admin.cursor()
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (scratch,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
        cur.execute(f'CREATE DATABASE "{scratch}"')
    finally:
        admin.close()
    _bootstrap_schema(scratch)

    env = dict(os.environ)
    env.update(
        {
            "ENVIRONMENT": "development",
            "JAEGER_ENABLED": "false",
            "POSTGRES_HOST": PG["host"],
            "POSTGRES_PORT": str(PG["port"]),
            "POSTGRES_USER": PG["user"],
            "POSTGRES_PASSWORD": PG["password"],
            "POSTGRES_DB": scratch,
            "REDIS_PASSWORD": env.get("REDIS_PASSWORD", ""),
        }
    )
    stamp = _alembic(env, "stamp", "0003_spending_cap_fix")
    assert stamp.returncode == 0, stamp.stderr
    first = _alembic(env, "upgrade", "head")
    assert first.returncode == 0, first.stderr
    assert "0006_email_verified -> 0007_society_runtime" in first.stderr + first.stdout
    assert "0007_society_runtime -> 0008_society_phase2" in first.stderr + first.stdout
    assert "0008_society_phase2 -> 0009_app_tables" in first.stderr + first.stdout

    current = _alembic(env, "current")
    assert "0009_app_tables" in current.stdout + current.stderr, "alembic_version was not persisted"

    second = _alembic(env, "upgrade", "head")
    assert second.returncode == 0, second.stderr
    assert "Running upgrade" not in second.stderr + second.stdout, "migrations re-ran: version stamp did not persist"


@pytest.mark.parametrize(
    "table",
    ["society_events", "agent_runs", "agent_intents", "agent_capability_grants", "code_candidates", "intent_approvals"],
)
def test_orm_models_match_database_columns(engine, table):
    """Every ORM column exists in the DB and vice versa (catches SQL/ORM drift)."""
    from sqlalchemy import inspect

    from services.registry.app import models

    orm_table = models.Base.metadata.tables[table]
    db_cols = {c["name"] for c in inspect(engine).get_columns(table)}
    orm_cols = {c.name for c in orm_table.columns}
    assert orm_cols == db_cols, f"{table}: ORM-only={orm_cols - db_cols} DB-only={db_cols - orm_cols}"
