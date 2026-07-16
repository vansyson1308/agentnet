"""
Verify alembic baseline is wired up and the registry image runs migrations
on startup. Doesn't actually exercise migrations against a live DB —
that's covered by the build-and-test CI job.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def test_alembic_ini_present():
    assert (REPO / "services/registry/alembic.ini").exists()


def test_env_py_imports_models():
    text = (REPO / "services/registry/migrations/env.py").read_text()
    # env.py must import the SQLAlchemy Base + register all model modules
    # so autogenerate sees every table.
    assert "from app.database import Base" in text
    assert "import app.models" in text
    assert "from app.config import DATABASE_URL" in text


def test_baseline_revision_present():
    versions = REPO / "services/registry/migrations/versions"
    files = sorted(p.name for p in versions.glob("*.py"))
    assert "0001_baseline.py" in files
    assert any("idempotency" in f for f in files), files
    assert any("spending_cap" in f for f in files), files


@pytest.mark.parametrize(
    "revision_file",
    ["0001_baseline.py", "0002_idempotency_key.py", "0003_spending_cap_fix.py"],
)
def test_revision_chain_well_formed(revision_file):
    p = REPO / "services/registry/migrations/versions" / revision_file
    text = p.read_text()
    assert "revision = " in text
    assert "down_revision = " in text


def test_idempotency_migration_matches_init_db():
    mig = (REPO / "services/registry/migrations/versions/0002_idempotency_key.py").read_text()
    sql = (REPO / "services/registry/init-db/13-idempotency.sql").read_text()
    # Both must touch the same column + index.
    assert "idempotency_key" in mig
    assert "idempotency_key" in sql
    assert "idx_transactions_idempotency_key" in mig
    assert "idx_transactions_idempotency_key" in sql


def test_dockerfile_runs_alembic_on_startup():
    docker = (REPO / "services/registry/Dockerfile").read_text()
    assert "entrypoint.sh" in docker
    entry = (REPO / "services/registry/entrypoint.sh").read_text()
    assert "alembic upgrade head" in entry
    assert "alembic stamp" in entry  # baseline-stamp branch


def test_online_migrations_commit_before_advisory_unlock():
    env = (REPO / "services/registry/migrations/env.py").read_text()
    run_index = env.index("context.run_migrations()")
    commit_index = env.index("connection.commit()", run_index)
    unlock_index = env.index("pg_advisory_unlock", commit_index)
    assert run_index < commit_index < unlock_index
    assert 'connection.execute(text("ROLLBACK"))' not in env


def test_postgres_init_does_not_execute_operator_helper_twice():
    helper = (REPO / "services/registry/init-db/apply-pending.sh").read_text()
    assert "/docker-entrypoint-initdb.d/*" in helper
    assert "numbered SQL files already applied" in helper
    attributes = (REPO / ".gitattributes").read_text()
    assert "*.sh text eol=lf" in attributes
