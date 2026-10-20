"""speakers.notify_service

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("speakers", sa.Column("notify_service", sa.String(255)))


def downgrade() -> None:
    op.drop_column("speakers", "notify_service")
