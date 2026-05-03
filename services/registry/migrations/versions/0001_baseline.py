"""baseline — stamps the schema produced by init-db/*.sql

Revision ID: 0001_baseline
Revises:
Create Date: 2026-05-03

This revision exists so existing databases that were bootstrapped from
the ``init-db/*.sql`` bundle can ``alembic stamp head`` and start
consuming incremental migrations from this point forward. It is
deliberately empty — running ``upgrade()`` on a fresh DB does NOT
create the schema (use init-db/01-init.sql for bootstrap), it just
records the baseline revision in alembic_version.

Going forward all schema changes ship as new migration files in this
directory, applied via ``alembic upgrade head`` at container startup.
"""

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Intentionally empty — schema is bootstrapped by init-db/*.sql.
    pass


def downgrade() -> None:
    # Schema teardown is out of scope; recreate the DB if you really
    # need to roll back the baseline.
    pass
