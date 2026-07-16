"""
Alembic environment for the registry service.

The DATABASE_URL is taken from ``app.config`` which validates secrets
fail-fast. Migrations target ``app.database.Base.metadata`` so
autogenerate sees every SQLAlchemy model under the registry.

Usage:
    cd services/registry
    alembic upgrade head           # apply all pending migrations
    alembic revision -m "msg"      # new empty migration
    alembic revision --autogenerate -m "msg"
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import create_engine

# Postgres advisory-lock id used to serialise concurrent
# `alembic upgrade head` calls (e.g. multi-replica startup). 32-bit int
# constant, not derived from anything; just needs to be stable so every
# replica picks the same lock.
_ALEMBIC_LOCK_ID = 7654321

# Make sure the application is importable.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Make sure model modules register with Base.metadata.
import app.models  # noqa: F401, E402
from app.config import DATABASE_URL  # noqa: E402
from app.database import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection — emits raw SQL."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations with a live DB connection.

    Wraps the run with a Postgres session-level advisory lock so two
    replicas booting simultaneously (e.g. ``docker compose up`` of two
    registry pods) cannot race and corrupt the schema. The second pod
    blocks on ``pg_advisory_lock`` until the first commits, then sees
    ``alembic upgrade head`` is a no-op.
    """
    connectable = create_engine(DATABASE_URL, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _ALEMBIC_LOCK_ID})
        # SQLAlchemy 2.x autobegins on the lock SELECT. Commit that transaction
        # before handing the connection to Alembic; the advisory lock is
        # session-scoped and remains held across the commit. Without this,
        # Alembic joins the lock transaction and a cleanup rollback silently
        # discards every migration while still reporting success.
        connection.commit()
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
            )
            with context.begin_transaction():
                context.run_migrations()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            # Release even if migrations raised; otherwise the next pod in a
            # rolling restart blocks until this connection closes.
            if connection.in_transaction():
                connection.rollback()
            try:
                connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _ALEMBIC_LOCK_ID})
                connection.commit()
            except Exception:
                connection.rollback()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
