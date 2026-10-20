"""auth: users, refresh_tokens, audit_log (append-only)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("entra_oid", sa.String(64), nullable=True),
        sa.Column("display_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("role IN ('admin', 'staff', 'student')", name="ck_users_role"),
        sa.UniqueConstraint("entra_oid", name="uq_users_entra_oid"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_table(
        "refresh_tokens",
        sa.Column("jti", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("actor", sa.String(320)),
        sa.Column("ip", sa.String(64)),
        sa.Column("detail", postgresql.JSONB),
    )
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.execute(
        """
        CREATE FUNCTION audit_log_immutable() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'audit_log is append-only'; END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_modify BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_immutable()"
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_log_no_truncate ON audit_log")
    op.execute("DROP TRIGGER audit_log_no_modify ON audit_log")
    op.execute("DROP FUNCTION audit_log_immutable()")
    op.drop_table("audit_log")
    op.drop_table("refresh_tokens")
    op.drop_table("users")
