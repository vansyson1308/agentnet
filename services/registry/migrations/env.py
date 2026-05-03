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
from sqlalchemy import pool
from sqlalchemy.engine import create_engine

# Make sure the application is importable.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import DATABASE_URL  # noqa: E402
from app.database import Base  # noqa: E402

# Make sure model modules register with Base.metadata.
import app.models  # noqa: F401, E402

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
    """Run migrations with a live DB connection."""
    connectable = create_engine(DATABASE_URL, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
