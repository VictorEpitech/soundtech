"""style de diction des annonces

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-23
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("announcements", sa.Column("style", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("announcements", "style")
