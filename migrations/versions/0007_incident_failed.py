"""incidents : état failed

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-20
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_incidents_status", "incidents", type_="check")
    op.create_check_constraint(
        "ck_incidents_status", "incidents", "status IN ('draft', 'sent', 'failed', 'closed')"
    )


def downgrade() -> None:
    op.execute("UPDATE incidents SET status = 'sent' WHERE status = 'failed'")
    op.drop_constraint("ck_incidents_status", "incidents", type_="check")
    op.create_check_constraint(
        "ck_incidents_status", "incidents", "status IN ('draft', 'sent', 'closed')"
    )
