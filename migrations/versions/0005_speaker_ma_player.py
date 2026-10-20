"""speakers.ma_player_id

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("speakers", sa.Column("ma_player_id", sa.String(255)))


def downgrade() -> None:
    op.drop_column("speakers", "ma_player_id")
