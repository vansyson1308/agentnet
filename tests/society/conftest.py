"""
Shared fixtures for the Autonomous Society Runtime tests.

These tests run against a REAL PostgreSQL database (the runtime's
correctness depends on row locks, ``FOR UPDATE SKIP LOCKED``, UNIQUE
constraints and the wallet triggers — none of which a mock can prove).

Connection settings come from the same env vars the services use
(``POSTGRES_HOST`` / ``POSTGRES_PORT`` / ``POSTGRES_USER`` /
``POSTGRES_PASSWORD``). A dedicated database named by
``SOCIETY_TEST_DB`` (default ``agentnet_society_test``) is dropped and
re-created once per session, bootstrapped from ``init-db/*.sql`` (the
same bundle Postgres runs on a fresh volume) plus the society DDL.

If Postgres is unreachable the whole package is SKIPPED with an explicit
reason — never silently passed. CI provides a Postgres service.
"""

from __future__ import annotations

import os
import pathlib
import sys
import uuid

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
REGISTRY_ROOT = REPO / "services" / "registry"
INIT_DB = REGISTRY_ROOT / "init-db"

# Make ``app.*`` importable the same way the registry service imports it
# (alembic env.py does the same). Tests import runtime code via
# ``services.registry.app.society`` to match the rest of the suite.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("JAEGER_ENABLED", "false")

PG = {
    "host": os.getenv("POSTGRES_HOST", "127.0.0.1"),
    "port": int(os.getenv("POSTGRES_PORT", "5432")),
    "user": os.getenv("POSTGRES_USER", "agentnet"),
    "password": os.getenv("POSTGRES_PASSWORD", ""),
}
MAINT_DB = os.getenv("POSTGRES_MAINT_DB", "postgres")
TEST_DB = os.getenv("SOCIETY_TEST_DB", "agentnet_society_test")

# Tables truncated before every test, in dependency-safe order (CASCADE
# handles the rest). Society tables first, then domain tables the runtime
# writes through (chat, goals, memory, proposals, tasks, wallets, agents).
TRUNCATE_TABLES = [
    "agent_intents",
    "code_candidates",
    "agent_runs",
    "society_events",
    "agent_capability_grants",
    "agent_chat",
    "memory_items",
    "improvement_proposals",
    "goals",
    "spans",
    "transactions",
    "daily_spending",
    "offers",
    "task_sessions",
    "wallets",
    "notifications",
    "agents",
    "users",
]


def _psycopg2():
    try:
        import psycopg2  # noqa: WPS433

        return psycopg2
    except ImportError as exc:  # pragma: no cover
        pytest.skip(f"psycopg2 not installed: {exc}")


def _admin_connection():
    psycopg2 = _psycopg2()
    conn = psycopg2.connect(dbname=MAINT_DB, connect_timeout=3, **PG)
    conn.autocommit = True
    return conn


def _bootstrap_schema(dbname: str) -> None:
    """Apply init-db/*.sql in lexical order (what the Postgres image does)."""
    psycopg2 = _psycopg2()
    conn = psycopg2.connect(dbname=dbname, **PG)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        for sql_file in sorted(INIT_DB.glob("*.sql")):
            cur.execute(sql_file.read_text(encoding="utf-8"))
        # Belt and braces: the society DDL is idempotent.
        sys.path.insert(0, str(REGISTRY_ROOT))
        from app.society.schema_sql import SOCIETY_RUNTIME_SQL  # noqa: E402

        cur.execute(SOCIETY_RUNTIME_SQL)
    finally:
        conn.close()
    # Tables that exist only as ORM models (no init-db DDL, e.g. projects,
    # scoped_tokens, audit_log): create them so the schema matches a
    # deployment where the ORM created them. Society tables are untouched
    # (already present via the SQL above; create_all skips existing tables).
    from sqlalchemy import create_engine

    from services.registry.app import models as _models

    pw = PG["password"]
    auth = f"{PG['user']}:{pw}@" if pw else f"{PG['user']}@"
    eng = create_engine(f"postgresql://{auth}{PG['host']}:{PG['port']}/{dbname}")
    try:
        _models.Base.metadata.create_all(eng)
    finally:
        eng.dispose()


@pytest.fixture(scope="session")
def society_db_url() -> str:
    try:
        admin = _admin_connection()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not reachable at {PG['host']}:{PG['port']} — {exc}")
    try:
        cur = admin.cursor()
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (TEST_DB,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        cur.execute(f'CREATE DATABASE "{TEST_DB}"')
    finally:
        admin.close()
    _bootstrap_schema(TEST_DB)
    pw = PG["password"]
    auth = f"{PG['user']}:{pw}@" if pw else f"{PG['user']}@"
    return f"postgresql://{auth}{PG['host']}:{PG['port']}/{TEST_DB}"


@pytest.fixture(scope="session")
def engine(society_db_url):
    from sqlalchemy import create_engine

    eng = create_engine(society_db_url, pool_pre_ping=True, pool_size=10, max_overflow=20)
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def SessionLocal(engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def clean_db(engine):
    """Truncate all runtime + domain tables so each test starts empty."""
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE " + ", ".join(TRUNCATE_TABLES) + " RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def db(clean_db, SessionLocal):
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def db_factory(clean_db, SessionLocal):
    """Factory for extra independent sessions (concurrency tests)."""
    created = []

    def _make():
        s = SessionLocal()
        created.append(s)
        return s

    yield _make
    for s in created:
        try:
            s.rollback()
        finally:
            s.close()


@pytest.fixture
def make_user(db):
    from services.registry.app.models import User

    def _make(email: str | None = None) -> User:
        user = User(
            id=uuid.uuid4(),
            email=email or f"u-{uuid.uuid4().hex[:8]}@society.test",
            password_hash="x",  # never used for login in these tests
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    return _make


@pytest.fixture
def make_agent(db, make_user):
    """Create an ACTIVE agent with a wallet (mirrors routes/agents.py)."""
    from services.registry.app.models import Agent, AgentStatus, Wallet, WalletOwnerType

    def _make(name: str, *, capabilities=None, balance_credits: int = 0, spending_cap: int = 1000, user=None, mission=None):
        owner = user or make_user()
        agent = Agent(
            id=uuid.uuid4(),
            user_id=owner.id,
            name=name,
            description=f"test agent {name}",
            capabilities=capabilities or [],
            endpoint=f"internal://test/{name}",
            public_key="test-public-key",
            status=AgentStatus.ACTIVE,
            mission=mission,
        )
        db.add(agent)
        db.flush()
        wallet = Wallet(
            id=uuid.uuid4(),
            owner_type=WalletOwnerType.AGENT,
            owner_id=agent.id,
            balance_credits=balance_credits,
            balance_usdc=0,
            reserved_credits=0,
            reserved_usdc=0,
            spending_cap=spending_cap,
        )
        db.add(wallet)
        db.commit()
        db.refresh(agent)
        return agent

    return _make


# ── engineering-loop helpers ──────────────────────────────────────────

ACCEPTANCE_REL = "tests/society/acceptance/test_candidate_docs.py"


def _git(args, cwd):
    import subprocess

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/tmp"),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True, check=True).stdout


def make_temp_repo(root: pathlib.Path) -> pathlib.Path:
    """A throw-away git repo that mirrors the parts of this repository the
    engineering loop needs (acceptance test + docs dir), so tests never
    create branches/worktrees in the developer's real checkout."""
    repo = root / "repo"
    repo.mkdir()
    (repo / "tests" / "society" / "acceptance").mkdir(parents=True)
    (repo / "tests" / "__init__.py").write_text("")
    (repo / "tests" / "society" / "__init__.py").write_text("")
    (repo / "tests" / "society" / "acceptance" / "__init__.py").write_text("")
    (repo / ACCEPTANCE_REL).write_text((REPO / ACCEPTANCE_REL).read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "docs" / "society" / "candidates").mkdir(parents=True)
    (repo / "docs" / "society" / "candidates" / "README.md").write_text("# candidates\n")
    (repo / "pytest.ini").write_text("[pytest]\naddopts = -q -p no:cacheprovider\n")
    (repo / "README.md").write_text("# temp repo\n")
    _git(["init", "-q", "-b", "main"], repo)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo


@pytest.fixture
def temp_repo(tmp_path):
    return make_temp_repo(tmp_path)


@pytest.fixture
def society_settings(tmp_path, temp_repo, monkeypatch):
    """Runtime settings with autonomy ON, pointing at the temp repo."""
    from services.registry.app.society.config import reset_settings_cache, SocietySettings

    monkeypatch.setenv("SOCIETY_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("SOCIETY_AUTONOMOUS_CODE_ENABLED", "true")
    monkeypatch.setenv("SOCIETY_MODEL_PROVIDER", "scripted")
    monkeypatch.setenv("SOCIETY_REPO_ROOT", str(temp_repo))
    monkeypatch.setenv("SOCIETY_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("SOCIETY_WAKE_POLL_SECONDS", "1")
    monkeypatch.setenv("SOCIETY_RETRY_BACKOFF_BASE_SECONDS", "0")
    monkeypatch.setenv("SOCIETY_QA_TEST_TIMEOUT_SECONDS", "120")
    reset_settings_cache()
    settings = SocietySettings()
    yield settings
    reset_settings_cache()


@pytest.fixture
def grants_with_no_cooldown(db):
    """After seeding, remove wake cooldowns so tests run fast."""
    from services.registry.app.models import AgentCapabilityGrant

    def _apply():
        for g in db.query(AgentCapabilityGrant).all():
            g.wake_cooldown_seconds = 0
        db.commit()

    return _apply
