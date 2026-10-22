"""annonces : voix/langue/ssml, préréglage par défaut, index d'historique

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("announcements", sa.Column("voice", sa.String(32)))
    op.add_column("announcements", sa.Column("lang", sa.String(16)))
    op.add_column("announcements", sa.Column("ssml", sa.Text))
    op.create_table(
        "announce_defaults",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("voice", sa.String(32)),
        sa.Column("lang", sa.String(16)),
        sa.CheckConstraint("id = 1", name="ck_announce_defaults_single"),
    )
    op.create_index("ix_announcements_status_created", "announcements", ["status", "created_at"])
    op.create_index("ix_announcements_author_email", "announcements", ["author_email"])
    op.execute(
        "CREATE INDEX ix_announcements_targets ON announcements USING gin (targets jsonb_path_ops)"
    )


def downgrade() -> None:
    op.drop_index("ix_announcements_targets", "announcements")
    op.drop_index("ix_announcements_author_email", "announcements")
    op.drop_index("ix_announcements_status_created", "announcements")
    op.drop_table("announce_defaults")
    op.drop_column("announcements", "ssml")
    op.drop_column("announcements", "lang")
    op.drop_column("announcements", "voice")
