"""A2A schema contract (ADR-0009 D5): one DDL source, two consumers.

The DB-backed ORM <-> database parity for these tables is covered by
``tests/test_db_parity.py`` (every registry ORM table is compared with the
bootstrapped database, and the alembic path is compared with the bundle).
"""

import pathlib
import re

from services.registry.app.a2a.schema_sql import A2A_SQL, A2A_TABLES, A2A_TASK_STATES, REMOTE_AGENT_STATES
from services.registry.app.society.schema_sql import SOCIETY_PHASE8_SQL

REGISTRY = pathlib.Path(__file__).resolve().parents[1] / "services" / "registry"


def test_init_db_18_is_byte_identical_to_the_schema_module():
    on_disk = (REGISTRY / "init-db" / "18-a2a-federation.sql").read_text(encoding="utf-8")
    assert on_disk == A2A_SQL, "init-db/18-a2a-federation.sql drifted from app/a2a/schema_sql.py -- regenerate it"


def test_bundle_order_puts_a2a_after_the_tables_it_references():
    names = sorted(p.name for p in (REGISTRY / "init-db").glob("*.sql"))
    assert names[-1] == "18-a2a-federation.sql"
    assert names.index("01-init.sql") < names.index("18-a2a-federation.sql")


def test_migration_0013_embeds_both_blocks_and_chains_from_0012():
    text = (REGISTRY / "migrations" / "versions" / "0013_a2a_federation.py").read_text(encoding="utf-8")
    assert 'revision = "0013_a2a_federation"' in text
    assert 'down_revision = "0012_memory_validation_history"' in text
    assert "op.execute(A2A_SQL)" in text and "op.execute(SOCIETY_PHASE8_SQL)" in text


def test_every_create_is_idempotent_and_additive():
    for sql in (A2A_SQL, SOCIETY_PHASE8_SQL):
        for line in sql.splitlines():
            if re.match(r"CREATE (UNIQUE )?(TABLE|INDEX)", line):
                assert "IF NOT EXISTS" in line, line
            assert not re.match(r"\s*(ALTER TABLE|DROP TABLE|DELETE FROM|UPDATE )", line), f"not additive: {line}"


def test_ddl_creates_exactly_the_declared_tables():
    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", A2A_SQL))
    assert created == set(A2A_TABLES)


def test_state_checks_match_the_python_constants():
    block = re.search(r"CONSTRAINT a2a_tasks_state_valid CHECK \(state IN \((.*?)\)\)", A2A_SQL, re.S).group(1)
    assert set(re.findall(r"'([A-Z_]+)'", block)) == set(A2A_TASK_STATES)
    block = re.search(r"CONSTRAINT a2a_remote_agents_state_valid CHECK \(state IN \((.*?)\)\)", A2A_SQL, re.S).group(1)
    assert set(re.findall(r"'([a-z]+)'", block)) == set(REMOTE_AGENT_STATES)


def test_audit_log_is_append_only_by_trigger():
    assert "BEFORE UPDATE OR DELETE ON a2a_audit_log" in A2A_SQL
    assert "RAISE EXCEPTION 'a2a_audit_log is append-only" in A2A_SQL


def test_no_money_column_exists_in_a2a_tables():
    """A2A never holds balances: escrow stays in task_sessions/transactions/wallets."""
    for word in ("balance", "reserved_credits", "reserved_usdc", "spending_cap"):
        assert word not in A2A_SQL
