"""
SQLAlchemy 2.0 declarative base (Phase 2.6 §6.5).

Each service's ``Base`` came from ``sqlalchemy.ext.declarative.declarative_base()``
— a 1.x import that SQLAlchemy 2.0 reports as ``MovedIn20Warning`` (pytest.ini
now makes that an error). Every service declares ``class Base(DeclarativeBase)``
instead. The metadata identity/parity contract is unchanged: each service's
models attach to exactly ONE registry/metadata, the migration env and the
society DDL keep seeing the same ``Base.metadata``, and the real-Postgres
parity suite (``tests/test_db_parity.py``) still compares that metadata
against the DDL.
"""

from __future__ import annotations

import importlib
import os
import pathlib

import pytest
from sqlalchemy.orm import DeclarativeBase

os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("JAEGER_ENABLED", "false")

REPO = pathlib.Path(__file__).resolve().parent.parent
EXPECTED_TABLES = {
    "registry": {"users", "agents", "wallets", "task_sessions", "transactions", "spans", "society_events"},
    "payment": {"wallets", "transactions"},
    "worker": {"task_sessions", "wallets"},
    "simulation": set(),
}


@pytest.mark.parametrize("svc", sorted(EXPECTED_TABLES))
def test_base_is_a_2_0_declarative_base_with_one_metadata(svc):
    database = importlib.import_module(f"services.{svc}.app.database")
    models = importlib.import_module(f"services.{svc}.app.models")
    assert issubclass(database.Base, DeclarativeBase)
    assert models.Base is database.Base, "models must attach to the service's single Base"
    tables = set(database.Base.metadata.tables)
    assert tables, f"{svc}: no tables mapped on Base.metadata"
    assert EXPECTED_TABLES[svc] <= tables, EXPECTED_TABLES[svc] - tables
    assert database.Base.registry.metadata is database.Base.metadata


@pytest.mark.parametrize("svc", sorted(EXPECTED_TABLES))
def test_no_legacy_declarative_import(svc):
    text = (REPO / "services" / svc / "app" / "database.py").read_text(encoding="utf-8")
    assert "sqlalchemy.ext.declarative" not in text
    assert "declarative_base()" not in text
    assert "class Base(DeclarativeBase)" in text


def test_alembic_env_uses_the_registry_base_metadata():
    text = (REPO / "services/registry/migrations/env.py").read_text(encoding="utf-8")
    assert "from app.database import Base" in text
    assert "Base.metadata" in text
