"""annuaire étudiants (import CSV, admin)

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "students",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("last_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("first_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("promo", sa.String(100), nullable=False, server_default=""),
        sa.Column("cursus", sa.String(100), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("email = lower(email)", name="ck_students_email_lower"),
    )
    op.create_index("ix_students_email", "students", ["email"], unique=True)
    op.create_index("ix_students_promo", "students", ["promo"])
    op.create_index("ix_students_cursus", "students", ["cursus"])


def downgrade() -> None:
    op.drop_table("students")
