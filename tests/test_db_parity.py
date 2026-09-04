"""
Database schema contract — init-db bundle vs alembic vs the four service ORMs.

See docs/DATABASE_SCHEMA_CONTRACT.md. What is proven here:

1. ``init-db/17-app-tables.sql`` is byte-identical to ``app/schema_app_sql.py``
   (migration 0009 embeds the same module) — the eight previously ORM-only
   tables now have exactly one DDL source.
2. Two scratch databases built two different ways end with IDENTICAL schemas:
     (A) FRESH   — the whole init-db bundle in lexical order, applied by
                   ``app.db_bootstrap`` (what a fresh volume / an empty managed
                   database gets);
     (B) UPGRADE — init-db files 01..15 only (a pre-society snapshot), then
                   ``alembic stamp 0003`` + ``alembic upgrade head``.
   Compared: tables, columns (type/udt/nullability/default/length/precision),
   primary keys, unique/check/foreign-key constraints, indexes, triggers and
   enum labels. Running alembic on top of (A) changes nothing, and
   ``downgrade 0008 -> upgrade head`` round-trips.
3. Every ORM column of registry / payment / worker / simulation exists in the
   DB with a compatible type and identical nullability; primary keys match;
   declared unique constraints exist; DB enum labels == Python enum values
   (TEXT+CHECK pseudo-enums compared against the CHECK constraint). Where the
   ORM is the full definition of a table, DB columns missing from the ORM are
   failures; service-specific subsets may omit only nullable/defaulted columns.
4. ``python -m app.db_bootstrap`` brings an EMPTY database to the full schema
   and the entrypoint's ``stamp 0003`` + ``upgrade head`` then lands on head.
5. ``agent_reputation_history`` (composite PK) accepts both the ORM insert and
   the ``ON CONFLICT`` upsert used by ``reputation.record_reputation_snapshot``;
   the heartbeat columns on ``agents`` persist through the ORM.

Skips with an explicit reason ONLY when PostgreSQL is unreachable.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import UniqueConstraint
from sqlalchemy import types as sat
from sqlalchemy.dialects import postgresql as pg_types

REPO = pathlib.Path(__file__).resolve().parent.parent
REGISTRY = REPO / "services" / "registry"
INIT_DB = REGISTRY / "init-db"

os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("JAEGER_ENABLED", "false")

PG = {
    "host": os.getenv("POSTGRES_HOST", "127.0.0.1"),
    "port": int(os.getenv("POSTGRES_PORT", "5432")),
    "user": os.getenv("POSTGRES_USER", "agentnet"),
    "password": os.getenv("POSTGRES_PASSWORD", ""),
}
MAINT_DB = os.getenv("POSTGRES_MAINT_DB", "postgres")
FRESH_DB = os.getenv("PARITY_FRESH_DB", "agentnet_parity_fresh")
UPGRADE_DB = os.getenv("PARITY_UPGRADE_DB", "agentnet_parity_upgrade")
BOOTSTRAP_DB = os.getenv("PARITY_BOOTSTRAP_DB", "agentnet_parity_bootstrap")

EXPECTED_HEAD = "0009_app_tables"
PRE_SOCIETY_MAX_PREFIX = 15  # init-db files 01..15 = schema before the society runtime + app tables

# The eight tables that had no DDL before app/schema_app_sql.py.
APP_TABLES = {
    "audit_log",
    "provisioning_providers",
    "provisioning_services",
    "projects",
    "scoped_tokens",
    "project_resources",
    "orchestrator_partners",
    "approval_requests",
}

# DB base tables that intentionally have no ORM model anywhere.
NO_ORM_TABLES = {
    "daily_spending": "written only by the check_spending_cap / update_daily_spending triggers",
    "alembic_version": "alembic bookkeeping",
}
# agent_connection_strength (05-social-graph.sql) is a MATERIALIZED VIEW, not a base table.

# Enum types created by the SQL bundle; every one must be bound by an ORM column.
DB_ENUM_TYPES = {
    "kyc_status",
    "agent_status",
    "wallet_owner_type",
    "task_status",
    "span_status",
    "transaction_status",
    "transaction_type",
    "referral_status",
    "offer_status",
    "currency_type",
    "interaction_type",
    "sim_status",
}

# Tables that use TEXT + CHECK pseudo-enums; every Enum column there must have a CHECK.
TEXT_CHECK_ENUM_TABLES = {"goals", "improvement_proposals", "memory_items"}

MUST_COVER = [
    "users", "agents", "wallets", "task_sessions", "transactions", "spans", "agent_chat", "goals",
    "memory_items", "improvement_proposals", "agent_reputation_history", "society_events", "agent_runs",
    "agent_intents", "agent_capability_grants", "code_candidates", "intent_approvals", "approval_requests",
    "scoped_tokens", "projects", "project_resources", "orchestrator_partners", "audit_log",
    "provisioning_providers", "provisioning_services", "offers", "negotiation_rounds", "referrals",
    "notifications", "stories", "email_verification_tokens", "agent_interactions", "sim_sessions",
    "sim_agent_profiles", "sim_results", "sim_reports", "sim_chat_messages",
]

ORM_MODULES = {
    "registry": "services.registry.app.models",
    "payment": "services.payment.app.models",
    "worker": "services.worker.app.models",
    "simulation": "services.simulation.app.models",
}


# ─────────────────────────────────────────────────────────────────────────
# Static / no-database tests
# ─────────────────────────────────────────────────────────────────────────


def _schema_module():
    from services.registry.app import schema_app_sql

    return schema_app_sql


def test_init_db_17_matches_schema_module():
    on_disk = (INIT_DB / "17-app-tables.sql").read_text(encoding="utf-8")
    assert on_disk == _schema_module().APP_TABLES_SQL, (
        "init-db/17-app-tables.sql drifted from app/schema_app_sql.py — regenerate it "
        "(python -c 'from app.schema_app_sql import APP_TABLES_SQL; ...')"
    )


def test_migration_0009_embeds_schema_module_and_chains_from_0008():
    text = (REGISTRY / "migrations" / "versions" / "0009_app_tables.py").read_text(encoding="utf-8")
    assert 'revision = "0009_app_tables"' in text
    assert 'down_revision = "0008_society_phase2"' in text
    assert "APP_TABLES_SQL" in text
    assert "APP_TABLES" in text  # downgrade drops exactly the module's table list


def test_app_tables_ddl_is_idempotent_and_covers_every_orm_only_table():
    mod = _schema_module()
    created = set()
    for line in mod.APP_TABLES_SQL.splitlines():
        if line.startswith("CREATE TABLE") or line.startswith("CREATE INDEX") or line.startswith("CREATE UNIQUE INDEX"):
            assert "IF NOT EXISTS" in line, line
        m = re.match(r"CREATE TABLE IF NOT EXISTS (\w+)", line)
        if m:
            created.add(m.group(1))
    assert created == APP_TABLES
    assert set(mod.APP_TABLES) == APP_TABLES


@pytest.mark.parametrize("table", sorted(APP_TABLES))
def test_app_tables_ddl_lists_every_orm_column(table):
    """Cheap textual check (the DB-backed tests below are the real proof)."""
    import importlib

    module = ORM_MODULES["payment"] if table == "approval_requests" else ORM_MODULES["registry"]
    orm_table = importlib.import_module(module).Base.metadata.tables[table]
    sql = _schema_module().APP_TABLES_SQL
    block = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", sql, re.S).group(1)
    declared = {re.match(r"\s*(\w+)", line).group(1) for line in block.splitlines() if line.strip()}
    assert declared == {c.name for c in orm_table.columns}, table


def test_entrypoint_and_image_bootstrap_empty_databases():
    entry = (REGISTRY / "entrypoint.sh").read_text(encoding="utf-8")
    docker = (REGISTRY / "Dockerfile").read_text(encoding="utf-8")
    assert 'python -m app.db_bootstrap --init-dir "${INIT_DB_DIR}"' in entry
    assert 'INIT_DB_DIR="${INIT_DB_DIR:-/app/init-db}"' in entry, "INIT_DB_DIR must default to the image path"
    assert "alembic stamp 0003_spending_cap_fix" in entry
    assert "alembic upgrade head" in entry
    assert "COPY init-db /app/init-db" in docker
    assert "17-app-tables.sql" in entry, "entrypoint comment must name the current end of the bundle"
    assert "through 14-spending-cap-fix" not in entry, "stale comment: the bundle no longer stops at 14"


@pytest.mark.parametrize("service", sorted(ORM_MODULES))
def test_orm_enums_never_create_their_own_types(service):
    """Registry/payment/worker bind strings (native_enum=False); the simulation
    service references the pre-existing ``sim_status`` type with create_type=False.
    Either way no service can emit CREATE TYPE with an auto-derived name."""
    import importlib

    md = importlib.import_module(ORM_MODULES[service]).Base.metadata
    seen = 0
    for table in md.tables.values():
        for col in table.columns:
            if not isinstance(col.type, sat.Enum):
                continue
            seen += 1
            if col.type.native_enum:
                # Generic sqlalchemy.Enum silently drops create_type; only the
                # PG-dialect impl carries it, so inspect that.
                impl = col.type.dialect_impl(pg_types.dialect())
                assert getattr(impl, "create_type", True) is False, f"{service}.{table.name}.{col.name} would CREATE TYPE"
                assert col.type.name in DB_ENUM_TYPES, f"{service}.{table.name}.{col.name}: unknown type {col.type.name}"
            else:
                assert col.type.enums == [e.value for e in col.type.enum_class], f"{table.name}.{col.name}"
    assert seen > 0


# ─────────────────────────────────────────────────────────────────────────
# Database helpers
# ─────────────────────────────────────────────────────────────────────────


def _psycopg2():
    try:
        import psycopg2

        return psycopg2
    except ImportError as exc:  # pragma: no cover
        pytest.skip(f"psycopg2 not installed: {exc}")


def _dsn(dbname: str) -> str:
    pw = PG["password"]
    auth = f"{PG['user']}:{pw}@" if pw else f"{PG['user']}@"
    return f"postgresql://{auth}{PG['host']}:{PG['port']}/{dbname}"


def _admin_connection():
    psycopg2 = _psycopg2()
    conn = psycopg2.connect(dbname=MAINT_DB, connect_timeout=3, **PG)
    conn.autocommit = True
    return conn


def _recreate_database(name: str) -> None:
    admin = _admin_connection()
    try:
        cur = admin.cursor()
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        admin.close()


def _drop_database(name: str) -> None:
    try:
        admin = _admin_connection()
    except Exception:  # noqa: BLE001 — best effort cleanup
        return
    try:
        cur = admin.cursor()
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        admin.close()


def _alembic_env(dbname: str) -> dict:
    env = dict(os.environ)
    env.update(
        {
            "ENVIRONMENT": "development",
            "JAEGER_ENABLED": "false",
            "POSTGRES_HOST": PG["host"],
            "POSTGRES_PORT": str(PG["port"]),
            "POSTGRES_USER": PG["user"],
            "POSTGRES_PASSWORD": PG["password"],
            "POSTGRES_DB": dbname,
            "REDIS_PASSWORD": env.get("REDIS_PASSWORD", ""),
        }
    )
    return env


def _alembic(dbname: str, *args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REGISTRY,
        env=_alembic_env(dbname),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"alembic {' '.join(args)} failed on {dbname}:\n{proc.stdout}\n{proc.stderr}"
    return proc


def _alembic_current(dbname: str) -> str:
    proc = _alembic(dbname, "current")
    return proc.stdout + proc.stderr


def _bundle_files(max_prefix: int | None = None) -> list[pathlib.Path]:
    files = sorted(p for p in INIT_DB.iterdir() if p.suffix == ".sql")
    if max_prefix is None:
        return files
    return [p for p in files if int(re.match(r"(\d+)", p.name).group(1)) <= max_prefix]


def _apply_files(dbname: str, files) -> None:
    psycopg2 = _psycopg2()
    conn = psycopg2.connect(dbname=dbname, **PG)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        for path in files:
            cur.execute(path.read_text(encoding="utf-8"))
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────
# Schema snapshot (normalized dict) + diff
# ─────────────────────────────────────────────────────────────────────────


def snapshot_schema(dbname: str) -> dict:
    psycopg2 = _psycopg2()
    conn = psycopg2.connect(dbname=dbname, **PG)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name"
        )
        tables = [r[0] for r in cur.fetchall() if r[0] != "alembic_version"]
        snap = {"tables": {}, "enums": {}}
        for t in tables:
            cur.execute(
                "SELECT column_name, data_type, udt_name, is_nullable, column_default, "
                "character_maximum_length, numeric_precision, numeric_scale "
                "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s "
                "ORDER BY column_name",
                (t,),
            )
            columns = {
                r[0]: {
                    "data_type": r[1],
                    "udt_name": r[2],
                    "is_nullable": r[3],
                    "column_default": r[4],
                    "char_len": r[5],
                    "num_precision": r[6],
                    "num_scale": r[7],
                }
                for r in cur.fetchall()
            }
            cur.execute(
                "SELECT conname, contype, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = %s::regclass ORDER BY conname",
                (f"public.{t}",),
            )
            cons = cur.fetchall()
            by_type = {"p": {}, "u": {}, "c": {}, "f": {}}
            for name, ctype, cdef in cons:
                by_type.setdefault(ctype, {})[name] = cdef
            cur.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = %s "
                "ORDER BY indexname",
                (t,),
            )
            indexes = dict(cur.fetchall())
            cur.execute(
                "SELECT ic.relname, i.indisunique, array_agg(a.attname ORDER BY k.ord) "
                "FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
                "JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE "
                "LEFT JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
                "WHERE i.indrelid = %s::regclass GROUP BY ic.relname, i.indisunique",
                (f"public.{t}",),
            )
            index_columns = {name: {"unique": uniq, "columns": [c for c in cols if c]} for name, uniq, cols in cur.fetchall()}
            cur.execute(
                "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger "
                "WHERE tgrelid = %s::regclass AND NOT tgisinternal ORDER BY tgname",
                (f"public.{t}",),
            )
            triggers = dict(cur.fetchall())
            snap["tables"][t] = {
                "columns": columns,
                "primary_key": by_type["p"],
                "unique": by_type["u"],
                "check": by_type["c"],
                "foreign_keys": by_type["f"],
                "indexes": indexes,
                "index_columns": index_columns,
                "triggers": triggers,
            }
        cur.execute(
            "SELECT t.typname, e.enumlabel FROM pg_type t "
            "JOIN pg_enum e ON e.enumtypid = t.oid "
            "JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE n.nspname = 'public' ORDER BY t.typname, e.enumsortorder"
        )
        for typname, label in cur.fetchall():
            snap["enums"].setdefault(typname, []).append(label)
        return snap
    finally:
        conn.close()


def diff_schemas(a, b, path: str = "") -> list[str]:
    """Human-readable list of differences between two snapshots (empty == identical)."""
    diffs: list[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            sub = f"{path}/{key}" if path else str(key)
            if key not in a:
                diffs.append(f"{sub}: only in B ({b[key]!r})")
            elif key not in b:
                diffs.append(f"{sub}: only in A ({a[key]!r})")
            else:
                diffs.extend(diff_schemas(a[key], b[key], sub))
    elif a != b:
        diffs.append(f"{path}: A={a!r} B={b!r}")
    return diffs


def _constraint_columns(cdef: str) -> set[str]:
    """``PRIMARY KEY (a, b)`` / ``UNIQUE (a, b)`` -> {a, b}."""
    inner = re.search(r"\(([^)]*)\)", cdef).group(1)
    return {c.strip().strip('"') for c in inner.split(",")}


# ─────────────────────────────────────────────────────────────────────────
# Fixtures (session-scoped scratch databases)
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def pg():
    try:
        _admin_connection().close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not reachable at {PG['host']}:{PG['port']} — {exc}")
    return PG


@pytest.fixture(scope="session")
def fresh_db(pg):
    """(A) The whole bundle, applied by app.db_bootstrap (what the entrypoint
    does on an empty database; what the Postgres image does on a fresh volume)."""
    from services.registry.app import db_bootstrap

    _recreate_database(FRESH_DB)
    log: list[str] = []
    result = db_bootstrap.bootstrap(_dsn(FRESH_DB), INIT_DB, log=log.append)
    assert result.applied, log
    assert result.files == [p.name for p in _bundle_files()]
    yield FRESH_DB
    _drop_database(FRESH_DB)


@pytest.fixture(scope="session")
def upgrade_db(pg):
    """(B) Pre-society snapshot (files 01..15) + alembic stamp 0003 + upgrade head."""
    _recreate_database(UPGRADE_DB)
    pre = _bundle_files(PRE_SOCIETY_MAX_PREFIX)
    assert pre and pre[-1].name.startswith("15-") and not any(p.name.startswith(("16-", "17-")) for p in pre)
    _apply_files(UPGRADE_DB, pre)
    _alembic(UPGRADE_DB, "stamp", "0003_spending_cap_fix")
    up = _alembic(UPGRADE_DB, "upgrade", "head")
    out = up.stdout + up.stderr
    for hop in (
        "0003_spending_cap_fix -> 0004_balance_checks",
        "0006_email_verified -> 0007_society_runtime",
        "0007_society_runtime -> 0008_society_phase2",
        "0008_society_phase2 -> 0009_app_tables",
    ):
        assert hop in out, out
    assert EXPECTED_HEAD in _alembic_current(UPGRADE_DB)
    yield UPGRADE_DB
    _drop_database(UPGRADE_DB)


@pytest.fixture(scope="session")
def fresh_snapshot(fresh_db):
    return snapshot_schema(fresh_db)


@pytest.fixture(scope="session")
def fresh_engine(fresh_db):
    from sqlalchemy import create_engine

    eng = create_engine(_dsn(fresh_db), pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture
def fresh_session(fresh_engine):
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(autocommit=False, autoflush=False, bind=fresh_engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ─────────────────────────────────────────────────────────────────────────
# (A) bundle == (B) alembic
# ─────────────────────────────────────────────────────────────────────────


def test_fresh_bundle_and_alembic_upgrade_produce_identical_schemas(fresh_snapshot, upgrade_db):
    diffs = diff_schemas(fresh_snapshot, snapshot_schema(upgrade_db))
    assert not diffs, "init-db bundle (A) != alembic upgrade path (B):\n" + "\n".join(diffs)


def test_fresh_snapshot_contains_every_expected_table_and_enum(fresh_snapshot):
    missing = [t for t in MUST_COVER if t not in fresh_snapshot["tables"]]
    assert not missing, missing
    assert set(fresh_snapshot["enums"]) == DB_ENUM_TYPES
    assert "daily_spending" in fresh_snapshot["tables"]


def test_alembic_on_top_of_fresh_bundle_is_a_schema_noop(fresh_db, fresh_snapshot):
    """The entrypoint always runs stamp 0003 + upgrade head after the bundle."""
    _alembic(fresh_db, "stamp", "0003_spending_cap_fix")
    up = _alembic(fresh_db, "upgrade", "head")
    assert "-> 0009_app_tables" in up.stdout + up.stderr
    assert EXPECTED_HEAD in _alembic_current(fresh_db)
    diffs = diff_schemas(fresh_snapshot, snapshot_schema(fresh_db))
    assert not diffs, "alembic changed a bundle-bootstrapped schema:\n" + "\n".join(diffs)
    second = _alembic(fresh_db, "upgrade", "head")
    assert "Running upgrade" not in second.stdout + second.stderr


def test_downgrade_0008_then_upgrade_head_round_trips(upgrade_db):
    before = snapshot_schema(upgrade_db)
    assert APP_TABLES <= set(before["tables"])
    _alembic(upgrade_db, "downgrade", "0008_society_phase2")
    assert "0008_society_phase2" in _alembic_current(upgrade_db)
    mid = snapshot_schema(upgrade_db)
    assert not (APP_TABLES & set(mid["tables"])), "downgrade left app tables behind"
    assert set(before["tables"]) - set(mid["tables"]) == APP_TABLES, "downgrade touched other tables"
    _alembic(upgrade_db, "upgrade", "head")
    assert EXPECTED_HEAD in _alembic_current(upgrade_db)
    diffs = diff_schemas(before, snapshot_schema(upgrade_db))
    assert not diffs, "downgrade/upgrade did not round-trip:\n" + "\n".join(diffs)


# ─────────────────────────────────────────────────────────────────────────
# ORM <-> DB
# ─────────────────────────────────────────────────────────────────────────


def _orm_metadata(service: str):
    import importlib

    return importlib.import_module(ORM_MODULES[service]).Base.metadata


def _authoritative_tables(service: str, md) -> set[str]:
    """Tables for which this service's ORM is the FULL definition."""
    if service in ("registry", "simulation"):
        return set(md.tables)
    if service == "payment":
        return {"approval_requests"}
    return set()


def _type_error(col, dbcol: dict) -> str | None:
    """None when the SQLAlchemy type is compatible with the DB column, else a message."""
    t = col.type
    udt, data_type = dbcol["udt_name"], dbcol["data_type"]
    if isinstance(t, (pg_types.UUID, sat.Uuid)):
        expected = {"uuid"}
    elif isinstance(t, sat.Enum):
        if t.native_enum:
            return None if (data_type == "USER-DEFINED" and udt == t.name) else f"expected enum type {t.name}, DB has {udt}"
        if data_type == "USER-DEFINED":
            return None  # labels are compared in test_enum_labels_match_python_enums
        expected = {"varchar", "text"}
    elif isinstance(t, sat.String):  # String, Text, VARCHAR ...
        expected = {"varchar", "text"}
        if t.length is not None and dbcol["char_len"] is not None and t.length != dbcol["char_len"]:
            return f"length {t.length} vs DB {dbcol['char_len']}"
    elif isinstance(t, sat.BigInteger):
        expected = {"int8"}
    elif isinstance(t, sat.SmallInteger):
        expected = {"int2"}
    elif isinstance(t, sat.Integer):
        expected = {"int4"}
    elif isinstance(t, sat.Float):
        expected = {"float8"}
    elif isinstance(t, sat.Numeric):
        expected = {"numeric"}
        if t.precision is not None and (t.precision, t.scale) != (dbcol["num_precision"], dbcol["num_scale"]):
            return f"numeric({t.precision},{t.scale}) vs DB numeric({dbcol['num_precision']},{dbcol['num_scale']})"
    elif isinstance(t, sat.Boolean):
        expected = {"bool"}
    elif isinstance(t, sat.DateTime):
        expected = {"timestamptz"} if t.timezone else {"timestamp"}
    elif isinstance(t, sat.Date):
        expected = {"date"}
    elif isinstance(t, pg_types.JSONB):
        expected = {"jsonb"}
    elif isinstance(t, sat.JSON):
        expected = {"json", "jsonb"}
    else:
        return f"unmapped SQLAlchemy type {t!r}"
    return None if udt in expected else f"{t!r} expects {sorted(expected)}, DB has {udt}"


def _orm_cases():
    cases = []
    for service in sorted(ORM_MODULES):
        for name in sorted(_orm_metadata(service).tables):
            cases.append(pytest.param(service, name, id=f"{service}:{name}"))
    return cases


@pytest.mark.parametrize("service,table", _orm_cases())
def test_orm_table_matches_database(service, table, fresh_snapshot):
    md = _orm_metadata(service)
    orm_table = md.tables[table]
    assert table in fresh_snapshot["tables"], f"{service}.{table}: table missing from the database"
    db = fresh_snapshot["tables"][table]
    dbcols = db["columns"]
    problems: list[str] = []

    for col in orm_table.columns:
        if col.name not in dbcols:
            problems.append(f"{col.name}: declared in ORM, missing in DB")
            continue
        err = _type_error(col, dbcols[col.name])
        if err:
            problems.append(f"{col.name}: {err}")
        db_nullable = dbcols[col.name]["is_nullable"] == "YES"
        if bool(col.nullable) != db_nullable:
            problems.append(f"{col.name}: ORM nullable={col.nullable} but DB nullable={db_nullable}")

    orm_cols = {c.name for c in orm_table.columns}
    if table in _authoritative_tables(service, md):
        extra = set(dbcols) - orm_cols
        if extra:
            problems.append(f"DB columns not in the authoritative ORM: {sorted(extra)}")
    else:
        # Subset rule: a column may be omitted only if it is nullable or has a DB default.
        for cname, dbc in dbcols.items():
            if cname not in orm_cols and dbc["is_nullable"] == "NO" and dbc["column_default"] is None:
                problems.append(f"{cname}: NOT NULL without default but omitted by the {service} subset model")

    orm_pk = {c.name for c in orm_table.primary_key.columns}
    db_pk = set()
    for cdef in db["primary_key"].values():
        db_pk |= _constraint_columns(cdef)
    if orm_pk != db_pk:
        problems.append(f"primary key ORM={sorted(orm_pk)} DB={sorted(db_pk)}")

    db_unique_sets = {frozenset(_constraint_columns(cdef)) for cdef in db["unique"].values()}
    db_unique_sets |= {frozenset(v["columns"]) for v in db["index_columns"].values() if v["unique"]}
    declared = [frozenset(c.name for c in con.columns) for con in orm_table.constraints if isinstance(con, UniqueConstraint)]
    declared += [frozenset(c.name for c in idx.columns) for idx in orm_table.indexes if idx.unique]
    for cols in declared:
        if cols not in db_unique_sets:
            problems.append(f"declared UNIQUE {sorted(cols)} has no unique constraint/index in DB")

    assert not problems, f"{service}.{table}:\n  " + "\n  ".join(problems)


def test_every_database_table_is_owned_by_an_orm_or_allowlisted(fresh_snapshot):
    orm_tables = set()
    for service in ORM_MODULES:
        orm_tables |= set(_orm_metadata(service).tables)
    db_tables = set(fresh_snapshot["tables"])
    unowned = db_tables - orm_tables - set(NO_ORM_TABLES)
    assert not unowned, f"DB tables with no ORM model and no allowlist reason: {sorted(unowned)}"
    assert set(MUST_COVER) <= orm_tables


def _check_values_for(table_snapshot: dict, column: str) -> set[str] | None:
    """Values of a ``col = ANY (ARRAY['a'::text, ...])`` CHECK on ``column``, if any."""
    for cdef in table_snapshot["check"].values():
        m = re.search(rf"\(\(?{re.escape(column)} = ANY \(ARRAY\[(.*?)\]\)", cdef)
        if m:
            return set(re.findall(r"'([^']*)'::", m.group(1)))
    return None


def test_enum_labels_match_python_enums(fresh_snapshot):
    """DB enum types are the truth: every ORM Enum column bound to a Postgres enum
    must have exactly the type's labels; TEXT+CHECK pseudo-enums must match the
    CHECK constraint; every DB enum type must be bound by at least one column."""
    problems: list[str] = []
    bound_types: set[str] = set()
    for service in sorted(ORM_MODULES):
        for table in _orm_metadata(service).tables.values():
            if table.name not in fresh_snapshot["tables"]:
                continue
            tsnap = fresh_snapshot["tables"][table.name]
            for col in table.columns:
                if not isinstance(col.type, sat.Enum):
                    continue
                values = set(col.type.enums)
                dbc = tsnap["columns"].get(col.name)
                if dbc is None:
                    continue  # reported by test_orm_table_matches_database
                where = f"{service}.{table.name}.{col.name}"
                if dbc["data_type"] == "USER-DEFINED":
                    labels = set(fresh_snapshot["enums"].get(dbc["udt_name"], []))
                    bound_types.add(dbc["udt_name"])
                    if labels != values:
                        problems.append(f"{where}: DB enum {dbc['udt_name']} labels {sorted(labels)} != {sorted(values)}")
                else:
                    check_values = _check_values_for(tsnap, col.name)
                    if check_values is None:
                        if table.name in TEXT_CHECK_ENUM_TABLES:
                            problems.append(f"{where}: no CHECK constraint enumerating the allowed values")
                    elif check_values != values:
                        problems.append(f"{where}: CHECK values {sorted(check_values)} != {sorted(values)}")
    assert not problems, "\n".join(problems)
    assert bound_types == DB_ENUM_TYPES, f"DB enum types not bound by any ORM column: {sorted(DB_ENUM_TYPES - bound_types)}"


# ─────────────────────────────────────────────────────────────────────────
# Bootstrap of an EMPTY database (entrypoint exit-code-20 path)
# ─────────────────────────────────────────────────────────────────────────


def _run_bootstrap(dbname: str, init_dir: pathlib.Path, via_env: bool = False) -> subprocess.CompletedProcess:
    """``python -m app.db_bootstrap`` — with ``--init-dir``, or via ``INIT_DB_DIR`` only."""
    env = _alembic_env(dbname)
    env.pop("INIT_DB_DIR", None)
    args = [sys.executable, "-m", "app.db_bootstrap"]
    if via_env:
        env["INIT_DB_DIR"] = str(init_dir)
    else:
        args += ["--init-dir", str(init_dir)]
    return subprocess.run(args, cwd=REGISTRY, env=env, capture_output=True, text=True, timeout=180)


def _run_entrypoint(dbname: str, init_dir: pathlib.Path) -> subprocess.CompletedProcess:
    """``INIT_DB_DIR=<checkout>/init-db bash entrypoint.sh true`` from services/registry."""
    env = _alembic_env(dbname)
    env["INIT_DB_DIR"] = str(init_dir)
    return subprocess.run(
        ["bash", "entrypoint.sh", "true"], cwd=REGISTRY, env=env, capture_output=True, text=True, timeout=300
    )


def _table_names(dbname: str) -> set[str]:
    psycopg2 = _psycopg2()
    conn = psycopg2.connect(dbname=dbname, **PG)
    try:
        cur = conn.cursor()
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def test_db_bootstrap_cli_brings_empty_database_to_head(pg, tmp_path, fresh_snapshot):
    _recreate_database(BOOTSTRAP_DB)
    try:
        # A broken bundle must fail hard, name the file, and leave the DB empty.
        broken = tmp_path / "broken-init-db"
        broken.mkdir()
        (broken / "01-ok.sql").write_text("CREATE TABLE IF NOT EXISTS parity_probe (id INT);\n", encoding="utf-8")
        (broken / "02-bad.sql").write_text("CREATE TABLE IF NOT EXISTS oops (;\n", encoding="utf-8")
        (broken / "03-never.sql").write_text("CREATE TABLE IF NOT EXISTS never_created (id INT);\n", encoding="utf-8")
        bad = _run_bootstrap(BOOTSTRAP_DB, broken)
        assert bad.returncode == 1, bad.stdout + bad.stderr
        assert "02-bad.sql" in bad.stderr
        assert "never_created" not in _table_names(BOOTSTRAP_DB), "bootstrap continued past a failing file"
        _recreate_database(BOOTSTRAP_DB)

        first = _run_bootstrap(BOOTSTRAP_DB, INIT_DB)
        assert first.returncode == 0, first.stdout + first.stderr
        assert "db_bootstrap: applied" in first.stdout
        assert "17-app-tables.sql" in first.stdout

        # The entrypoint's follow-up on the exit-code-20 path.
        _alembic(BOOTSTRAP_DB, "stamp", "0003_spending_cap_fix")
        up = _alembic(BOOTSTRAP_DB, "upgrade", "head")
        assert "-> 0009_app_tables" in up.stdout + up.stderr
        assert EXPECTED_HEAD in _alembic_current(BOOTSTRAP_DB)

        tables = _table_names(BOOTSTRAP_DB)
        assert set(MUST_COVER) <= tables, sorted(set(MUST_COVER) - tables)
        assert "daily_spending" in tables and "alembic_version" in tables

        # Re-running is a no-op once alembic manages the database — and the
        # CLI honours INIT_DB_DIR when --init-dir is omitted.
        again = _run_bootstrap(BOOTSTRAP_DB, INIT_DB, via_env=True)
        assert again.returncode == 0 and "skipped-alembic-managed" in again.stdout, again.stdout + again.stderr

        diffs = diff_schemas(fresh_snapshot, snapshot_schema(BOOTSTRAP_DB))
        assert not diffs, "CLI bootstrap != in-process bootstrap:\n" + "\n".join(diffs)
    finally:
        _drop_database(BOOTSTRAP_DB)


def test_db_bootstrap_cli_reports_missing_init_dir(pg):
    proc = _run_bootstrap(BOOTSTRAP_DB, pathlib.Path("/nonexistent/init-db"), via_env=True)
    assert proc.returncode == 1 and "init-db directory not found" in proc.stderr


@pytest.mark.skipif(shutil.which("alembic") is None or shutil.which("bash") is None, reason="needs alembic + bash on PATH")
def test_entrypoint_bootstraps_empty_database_end_to_end(pg):
    """The real container entrypoint (exit-code-20 path) from a local checkout:
    INIT_DB_DIR=<checkout>/init-db bash entrypoint.sh true — bootstraps, stamps
    0003, upgrades to head; a second run is the exit-code-0 no-op path."""
    _recreate_database(BOOTSTRAP_DB)
    try:
        first = _run_entrypoint(BOOTSTRAP_DB, INIT_DB)
        out = first.stdout + first.stderr
        assert first.returncode == 0, out
        assert "empty DB" in out and "db_bootstrap: applied" in out and "-> 0009_app_tables" in out, out
        assert EXPECTED_HEAD in _alembic_current(BOOTSTRAP_DB)
        tables = _table_names(BOOTSTRAP_DB)
        assert set(MUST_COVER) <= tables, sorted(set(MUST_COVER) - tables)

        second = _run_entrypoint(BOOTSTRAP_DB, INIT_DB)
        out2 = second.stdout + second.stderr
        assert second.returncode == 0, out2
        assert "alembic already stamped" in out2 and "Running upgrade" not in out2 and "db_bootstrap" not in out2, out2
        assert EXPECTED_HEAD in _alembic_current(BOOTSTRAP_DB)
    finally:
        _drop_database(BOOTSTRAP_DB)


def test_db_bootstrap_skips_when_schema_present_but_unstamped(pg):
    """The entrypoint's exit-code-10 path (bundle ran on a fresh volume, alembic
    never stamped) must NOT re-apply the bundle."""
    from services.registry.app import db_bootstrap

    _recreate_database(BOOTSTRAP_DB)
    try:
        _apply_files(BOOTSTRAP_DB, _bundle_files(PRE_SOCIETY_MAX_PREFIX))
        result = db_bootstrap.bootstrap(_dsn(BOOTSTRAP_DB), INIT_DB, log=lambda _m: None)
        assert result.status == "skipped-schema-present"
        assert "society_events" not in _table_names(BOOTSTRAP_DB)
    finally:
        _drop_database(BOOTSTRAP_DB)


# ─────────────────────────────────────────────────────────────────────────
# Behavioural regressions for the aligned ORM
# ─────────────────────────────────────────────────────────────────────────


def _make_agent(session):
    from services.registry.app.models import Agent, AgentStatus, User

    user = User(id=uuid.uuid4(), email=f"parity-{uuid.uuid4().hex[:8]}@test.local", password_hash="x")
    session.add(user)
    session.flush()
    agent = Agent(
        id=uuid.uuid4(),
        user_id=user.id,
        name="parity-agent",
        capabilities=[],
        endpoint="internal://parity",
        public_key="pk",
        status=AgentStatus.ACTIVE,
    )
    session.add(agent)
    session.commit()
    return agent


def test_reputation_history_orm_insert_and_core_upsert(fresh_session):
    from sqlalchemy import text

    from services.registry.app.models import AgentReputationHistory
    from services.registry.app.reputation import record_reputation_snapshot

    agent = _make_agent(fresh_session)
    today = date.today()

    # ORM path: composite PK, no surrogate id, success_rate/created_at defaults.
    fresh_session.add(AgentReputationHistory(agent_id=agent.id, snapshot_date=today, reputation_tier="bronze"))
    fresh_session.commit()
    row = fresh_session.get(AgentReputationHistory, (agent.id, today))
    assert row is not None and row.success_rate == 0.0 and row.created_at is not None

    # Core path (reputation.py): ON CONFLICT (agent_id, snapshot_date) DO UPDATE — twice.
    record_reputation_snapshot(fresh_session, agent.id, "silver", 0.5)
    record_reputation_snapshot(fresh_session, agent.id, "gold", 0.9)
    fresh_session.expire_all()
    rows = fresh_session.query(AgentReputationHistory).filter_by(agent_id=agent.id).all()
    assert len(rows) == 1
    assert (rows[0].reputation_tier, rows[0].success_rate) == ("gold", 0.9)
    raw = fresh_session.execute(
        text("SELECT count(*) FROM agent_reputation_history WHERE agent_id = :a"), {"a": str(agent.id)}
    ).scalar()
    assert raw == 1


def test_agent_heartbeat_columns_persist_through_the_orm(fresh_session):
    """Regression: is_online / last_seen_at / current_capability were assigned by
    websocket_manager but absent from the ORM -> silently never written."""
    from sqlalchemy import text

    from services.registry.app.models import Agent

    agent = _make_agent(fresh_session)
    seen = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    agent.is_online = True
    agent.last_seen_at = seen
    agent.current_capability = "translate"
    fresh_session.commit()
    fresh_session.expire_all()
    persisted = fresh_session.execute(
        text("SELECT is_online, last_seen_at, current_capability FROM agents WHERE id = :id"), {"id": str(agent.id)}
    ).one()
    assert persisted[0] is True
    assert persisted[1] == seen
    assert persisted[2] == "translate"
    assert fresh_session.get(Agent, agent.id).is_online is True
