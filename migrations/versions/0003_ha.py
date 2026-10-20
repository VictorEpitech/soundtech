"""home assistant: speakers, presets, announcements

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "speakers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("entity_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("group", sa.String(100)),
        sa.UniqueConstraint("entity_id", name="uq_speakers_entity_id"),
    )
    op.create_table(
        "presets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
    )
    op.create_table(
        "announcements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "author_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("author_email", sa.String(320)),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("priority", sa.Integer, nullable=False),
        sa.Column("is_test", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("targets", postgresql.JSONB, nullable=False),
        sa.Column("results", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("priority IN (0, 1, 2)", name="ck_announcements_priority"),
        sa.CheckConstraint(
            "status IN ('queued', 'sending', 'done', 'failed')", name="ck_announcements_status"
        ),
    )
    op.create_index("ix_announcements_created_at", "announcements", ["created_at"])


def downgrade() -> None:
    op.drop_table("announcements")
    op.drop_table("presets")
    op.drop_table("speakers")
