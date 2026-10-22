"""suivi d'incident : relances auto, abonnements Web Push, notifications admin

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("incidents", sa.Column("auto_remind_interval", sa.Integer))
    op.add_column("incidents", sa.Column("auto_remind_max", sa.Integer))
    op.add_column(
        "incidents",
        sa.Column("auto_remind_count", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column(
        "incidents",
        sa.Column("auto_remind_failures", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column("incidents", sa.Column("auto_remind_last_at", sa.DateTime(timezone=True)))

    op.create_table(
        "push_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("endpoint", sa.String(2048), nullable=False, unique=True),
        sa.Column("p256dh", sa.String(255), nullable=False),
        sa.Column("auth", sa.String(255), nullable=False),
        sa.Column("user_agent", sa.String(255)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_ok_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_push_subscriptions_user_id", "push_subscriptions", ["user_id"])

    op.create_table(
        "incident_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "incident_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("incident_id", "key", name="uq_incident_notifications_key"),
    )
    op.create_index(
        "ix_incident_notifications_incident_id", "incident_notifications", ["incident_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_incident_notifications_incident_id", "incident_notifications")
    op.drop_table("incident_notifications")
    op.drop_index("ix_push_subscriptions_user_id", "push_subscriptions")
    op.drop_table("push_subscriptions")
    for c in (
        "auto_remind_last_at",
        "auto_remind_failures",
        "auto_remind_count",
        "auto_remind_max",
        "auto_remind_interval",
    ):
        op.drop_column("incidents", c)
