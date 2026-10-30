"""Society: ticket sources for the funnel (owner story, generator, rescope)

Revision ID: 0029_ticket_sources
Revises: 0028_model_cache_accounting
Create Date: 2026-10-29

``society_tickets.source`` also takes ``owner_story``, ``generator`` and ``rescope``, and
``society_tickets.origin`` names a generated ticket's sub-source (SOCIETY_TICKET_SOURCES_SQL).
"""

import pathlib
import sys

from alembic import op

revision = "0029_ticket_sources"
down_revision = "0028_model_cache_accounting"
branch_labels = None
depends_on = None

_APP = pathlib.Path(__file__).resolve().parents[2]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.society.schema_sql import SOCIETY_TICKET_SOURCES_SQL  # noqa: E402


def upgrade() -> None:
    op.execute(SOCIETY_TICKET_SOURCES_SQL)


def downgrade() -> None:
    op.execute("UPDATE society_tickets SET source = 'department' WHERE source IN ('owner_story', 'generator', 'rescope');")
    op.execute("ALTER TABLE society_tickets DROP CONSTRAINT IF EXISTS society_tickets_source_check;")
    op.execute("ALTER TABLE society_tickets ADD CONSTRAINT society_tickets_source_check "
               "CHECK (source IN ('owner', 'scout', 'incident', 'backlog', 'department'));")
    op.execute("ALTER TABLE society_tickets DROP COLUMN IF EXISTS origin;")
