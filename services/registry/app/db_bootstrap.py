"""Bootstrap an EMPTY PostgreSQL database from the ``init-db/*.sql`` bundle.

Why this exists
---------------
The Postgres image only runs ``/docker-entrypoint-initdb.d`` (our init-db
bundle) on a *fresh volume*. A managed or otherwise empty database (RDS,
Neon, a CI service container, ``createdb`` by hand) never runs it, and
migration ``0001_baseline`` is deliberately a no-op, so ``alembic upgrade
head`` on an empty database used to leave ZERO tables.

The registry entrypoint therefore calls this module before alembic whenever
the schema is absent::

    python -m app.db_bootstrap --init-dir "$INIT_DB_DIR"   # /app/init-db in the image
    alembic stamp 0003_spending_cap_fix     # bundle already contains 13/14 (= 0002/0003)
    alembic upgrade head                    # 0004.. are idempotent over the bundle

``--init-dir`` may be omitted, in which case the ``INIT_DB_DIR`` environment
variable is used (the entrypoint exports the same variable, default
``/app/init-db``), so a local checkout can run the entrypoint with
``INIT_DB_DIR=<checkout>/services/registry/init-db``.

Semantics (mirrors ``psql -v ON_ERROR_STOP=1 -f <file>`` per file):

* files are applied in lexical order, each as ONE multi-statement query in
  its own transaction — a failure rolls that file back, prints which file
  failed and exits non-zero; nothing after it runs;
* if the schema is already present (``public.transactions`` exists) or the
  database is already alembic-managed (``alembic_version`` exists), nothing
  is applied and the exit code is 0 — the entrypoint's normal
  stamp/upgrade flow takes over.

The module depends only on ``psycopg2`` (no ``app.config`` import) so it is
usable from tests and from the entrypoint before the app's fail-fast secret
validation runs. See docs/DATABASE_SCHEMA_CONTRACT.md.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Callable, List, Optional

# Table whose presence means "the bundle (or an equivalent) already ran".
SCHEMA_SENTINEL_TABLE = "transactions"
ALEMBIC_TABLE = "alembic_version"
INIT_DIR_ENV = "INIT_DB_DIR"
DEFAULT_INIT_DIR = "/app/init-db"


def _emit(message: str, stream=None) -> None:
    """Plain line output for the CLI. This module runs from the container
    entrypoint before the app's structured logging is configured, so it
    writes straight to stdout/stderr (no ``print`` — see
    tests/test_logging_config.py)."""
    stream = sys.stdout if stream is None else stream
    stream.write(message + "\n")
    stream.flush()


class BootstrapError(RuntimeError):
    """A bundle file failed; ``filename`` names it."""

    def __init__(self, filename: str, cause: BaseException):
        super().__init__(f"{filename}: {cause}")
        self.filename = filename
        self.cause = cause


@dataclass
class BootstrapResult:
    status: str  # "applied" | "skipped-schema-present" | "skipped-alembic-managed"
    files: List[str] = field(default_factory=list)

    @property
    def applied(self) -> bool:
        return self.status == "applied"


def build_dsn_from_env(env=None) -> str:
    """Same env vars the services use (``POSTGRES_*``)."""
    env = os.environ if env is None else env
    host = env.get("POSTGRES_HOST", "postgres")
    port = env.get("POSTGRES_PORT", "5432")
    user = env.get("POSTGRES_USER", "agentnet")
    password = env.get("POSTGRES_PASSWORD", "")
    dbname = env.get("POSTGRES_DB", "agentnet")
    auth = f"{user}:{password}@" if password else f"{user}@"
    return f"postgresql://{auth}{host}:{port}/{dbname}"


def sql_files(init_dir: pathlib.Path) -> List[pathlib.Path]:
    """``*.sql`` files in lexical order — exactly what the Postgres image does."""
    init_dir = pathlib.Path(init_dir)
    if not init_dir.is_dir():
        raise FileNotFoundError(f"init-db directory not found: {init_dir}")
    files = sorted(p for p in init_dir.iterdir() if p.is_file() and p.suffix == ".sql")
    if not files:
        raise FileNotFoundError(f"no *.sql files in {init_dir}")
    return files


def _regclass_exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
    return cur.fetchone()[0] is not None


def schema_present(conn) -> bool:
    with conn.cursor() as cur:
        return _regclass_exists(cur, SCHEMA_SENTINEL_TABLE)


def alembic_managed(conn) -> bool:
    with conn.cursor() as cur:
        return _regclass_exists(cur, ALEMBIC_TABLE)


def apply_bundle(conn, init_dir: pathlib.Path, log: Callable[[str], None] = _emit) -> List[str]:
    """Apply every bundle file, one transaction per file, fail hard on error."""
    applied: List[str] = []
    # End any transaction the presence checks opened (psycopg2 autobegins on
    # the first statement), then run in explicit per-file transactions.
    conn.rollback()
    if conn.autocommit:
        conn.autocommit = False
    for path in sql_files(init_dir):
        sql = path.read_text(encoding="utf-8")
        log(f"db_bootstrap: applying {path.name}")
        try:
            with conn.cursor() as cur:
                # No parameters -> psycopg2 does not interpret '%' in the SQL
                # (the trigger bodies contain RAISE ... '%' placeholders).
                cur.execute(sql)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 — re-raised with the file name
            conn.rollback()
            raise BootstrapError(path.name, exc) from exc
        applied.append(path.name)
    return applied


def bootstrap(
    dsn: str,
    init_dir: pathlib.Path,
    log: Callable[[str], None] = _emit,
    connect: Optional[Callable] = None,
) -> BootstrapResult:
    """Apply the bundle if (and only if) the database is empty."""
    sql_files(init_dir)  # fail fast on a missing/empty bundle dir before touching the DB
    if connect is None:
        import psycopg2

        connect = psycopg2.connect
    conn = connect(dsn)
    try:
        if alembic_managed(conn):
            log("db_bootstrap: alembic_version exists — database is migration-managed, nothing to do")
            return BootstrapResult("skipped-alembic-managed")
        if schema_present(conn):
            log(f"db_bootstrap: public.{SCHEMA_SENTINEL_TABLE} exists — schema present, nothing to do")
            return BootstrapResult("skipped-schema-present")
        files = apply_bundle(conn, init_dir, log)
        log(f"db_bootstrap: applied {len(files)} file(s) from {init_dir}")
        return BootstrapResult("applied", files)
    finally:
        conn.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.db_bootstrap",
        description="Apply init-db/*.sql to an EMPTY database (no-op when the schema exists).",
    )
    parser.add_argument(
        "--init-dir",
        default=None,
        help=f"directory containing the init-db *.sql bundle (default: ${INIT_DIR_ENV}, then {DEFAULT_INIT_DIR})",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="PostgreSQL DSN; defaults to POSTGRES_HOST/PORT/USER/PASSWORD/DB from the environment",
    )
    args = parser.parse_args(argv)
    init_dir = args.init_dir or os.environ.get(INIT_DIR_ENV) or DEFAULT_INIT_DIR
    dsn = args.dsn or build_dsn_from_env()
    try:
        result = bootstrap(dsn, pathlib.Path(init_dir))
    except BootstrapError as exc:
        _emit(f"db_bootstrap: FAILED in {exc.filename}: {exc.cause}", sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — connection / filesystem errors
        _emit(f"db_bootstrap: FAILED: {exc}", sys.stderr)
        return 1
    _emit(f"db_bootstrap: {result.status}")
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    sys.exit(main())
