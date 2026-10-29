"""Society: prompt-cache accounting per run

Revision ID: 0027_model_cache_accounting
Revises: 0026_owner_directives
Create Date: 2026-10-29

Additive: ``agent_runs.tokens_cache_write`` and ``agent_runs.tokens_cache_write_1h``
(SOCIETY_MODEL_CACHE_SQL): the prompt-cache writes of a run, next to ``tokens_cached`` (reads).
"""

import pathlib
import sys

from alembic import op

revision = "0027_model_cache_accounting"
down_revision = "0026_owner_directives"
branch_labels = None
depends_on = None

_APP = pathlib.Path(__file__).resolve().parents[2]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.society.schema_sql import SOCIETY_MODEL_CACHE_SQL  # noqa: E402


def upgrade() -> None:
    op.execute(SOCIETY_MODEL_CACHE_SQL)


def downgrade() -> None:
    op.execute("ALTER TABLE agent_runs DROP COLUMN IF EXISTS tokens_cache_write_1h;")
    op.execute("ALTER TABLE agent_runs DROP COLUMN IF EXISTS tokens_cache_write;")
