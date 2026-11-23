"""Per-person preferences (#723).

Revision ID: 034_user_prefs
Revises: 033_ui_agent_owners
"""

import sqlalchemy as sa

from alembic import op

revision = "034_user_prefs"
down_revision = "033_ui_agent_owners"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memoryhub_user_prefs",
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("prefs", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("history", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("notifications_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "user_id"),
    )


def downgrade() -> None:
    op.drop_table("memoryhub_user_prefs")
