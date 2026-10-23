"""speaker_groups, speaker_group_members, scenes, scene_speakers

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def _uuid(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), **kw)


def upgrade() -> None:
    op.create_table(
        "speaker_groups",
        _uuid("id", primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("description", sa.String(500)),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "speaker_group_members",
        _uuid("group_id", primary_key=True),
        _uuid("speaker_id", primary_key=True),
        sa.ForeignKeyConstraint(["group_id"], ["speaker_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["speaker_id"], ["speakers.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "scenes",
        _uuid("id", primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("icon", sa.String(64)),
        sa.Column("color", sa.String(7)),
        _uuid("group_id"),
        sa.Column("media_uri", sa.String(500), nullable=False),
        sa.Column("media_type", sa.String(16), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False, server_default="play"),
        sa.Column("volume", sa.Integer),
        sa.Column("shuffle", sa.Boolean),
        sa.Column("repeat", sa.String(8)),
        _uuid("preset_id"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.ForeignKeyConstraint(["group_id"], ["speaker_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["preset_id"], ["presets.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "media_type IN ('playlist', 'album', 'track', 'radio')", name="ck_scenes_media_type"
        ),
        sa.CheckConstraint("mode IN ('play', 'replace')", name="ck_scenes_mode"),
        sa.CheckConstraint(
            "volume IS NULL OR (volume >= 0 AND volume <= 100)", name="ck_scenes_volume"
        ),
        sa.CheckConstraint(
            "repeat IS NULL OR repeat IN ('off', 'one', 'all')", name="ck_scenes_repeat"
        ),
    )
    op.create_table(
        "scene_speakers",
        _uuid("scene_id", primary_key=True),
        _uuid("speaker_id", primary_key=True),
        sa.ForeignKeyConstraint(["scene_id"], ["scenes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["speaker_id"], ["speakers.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("scene_speakers")
    op.drop_table("scenes")
    op.drop_table("speaker_group_members")
    op.drop_table("speaker_groups")
