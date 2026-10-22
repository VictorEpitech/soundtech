"""responses (réponses étudiants aux incidents)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "responses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "incident_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_aad_id", sa.String(64), nullable=False),
        sa.Column("user_email", sa.String(320)),
        sa.Column("user_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("text", sa.Text),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("incident_id", "user_aad_id", name="uq_responses_incident_user"),
        sa.CheckConstraint(
            "status IN ('safe_campus', 'safe_offsite', 'danger')", name="ck_responses_status"
        ),
    )
    op.create_index("ix_responses_received_at", "responses", ["received_at"])


def downgrade() -> None:
    op.drop_table("responses")
