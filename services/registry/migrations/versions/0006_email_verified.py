"""email_verified — add is_email_verified column, backfill existing users

Revision ID: 0006_email_verified
Revises: 0005_platform_fee
Create Date: 2026-05-03 22:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006_email_verified'
down_revision: Union[str, None] = '0005_platform_fee'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add column (also set in init-db SQL, but ensure it exists)
    op.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS is_email_verified boolean DEFAULT false
    """)
    # Backfill all existing users as verified (they predate this requirement)
    op.execute("""
        UPDATE users SET is_email_verified = true
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE users DROP COLUMN IF EXISTS is_email_verified
    """)
