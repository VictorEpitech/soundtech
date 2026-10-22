"""planification : schedules, schedule_runs, closures, planning_settings

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def _uuid(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), **kw)


def upgrade() -> None:
    op.create_table(
        "schedules",
        _uuid("id", primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Europe/Paris"),
        sa.Column("days", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("time_of_day", sa.String(5), nullable=False),
        sa.Column("once_date", sa.Date),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("text", sa.Text),
        _uuid("preset_id"),
        sa.Column("voice", sa.String(32)),
        sa.Column("lang", sa.String(16)),
        sa.Column("group_ids", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("speaker_ids", postgresql.JSONB, nullable=False, server_default="[]"),
        _uuid("scene_id"),
        sa.Column("stop_volume", sa.Integer),
        sa.Column("is_test", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("respect_closures", sa.Boolean, nullable=False, server_default=sa.true()),
        _uuid("created_by"),
        sa.Column("created_by_email", sa.String(320)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "armed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["preset_id"], ["presets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["scene_id"], ["scenes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "action IN ('announcement', 'scene', 'stop')", name="ck_schedules_action"
        ),
        sa.CheckConstraint(
            "stop_volume IS NULL OR (stop_volume >= 0 AND stop_volume <= 100)",
            name="ck_schedules_stop_volume",
        ),
    )
    op.create_table(
        "schedule_runs",
        _uuid("id", primary_key=True),
        _uuid("schedule_id", nullable=False),
        sa.Column("schedule_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("trigger", sa.String(16), nullable=False, server_default="schedule"),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("detail", sa.String(255)),
        sa.ForeignKeyConstraint(["schedule_id"], ["schedules.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("schedule_id", "scheduled_for", name="uq_schedule_runs_slot"),
        sa.CheckConstraint(
            "status IN ('running', 'ok', 'skipped_closure', 'skipped_alert', 'failed', 'missed')",
            name="ck_schedule_runs_status",
        ),
        sa.CheckConstraint("trigger IN ('schedule', 'manual')", name="ck_schedule_runs_trigger"),
    )
    op.create_index("ix_schedule_runs_scheduled_for", "schedule_runs", ["scheduled_for"])
    op.create_table(
        "closures",
        _uuid("id", primary_key=True),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.CheckConstraint("end_date >= start_date", name="ck_closures_range"),
        sa.CheckConstraint("kind IN ('vacances', 'fermeture', 'ferie')", name="ck_closures_kind"),
    )
    op.create_index("ix_closures_range", "closures", ["start_date", "end_date"])
    op.create_table(
        "planning_settings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("observe_holidays", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.CheckConstraint("id = 1", name="ck_planning_settings_single"),
    )


def downgrade() -> None:
    op.drop_table("planning_settings")
    op.drop_index("ix_closures_range", "closures")
    op.drop_table("closures")
    op.drop_index("ix_schedule_runs_scheduled_for", "schedule_runs")
    op.drop_table("schedule_runs")
    op.drop_table("schedules")
