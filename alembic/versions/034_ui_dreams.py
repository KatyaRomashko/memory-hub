"""Dreaming runs and schedules for the dashboard (#716, Labs).

Revision ID: 034_ui_dreams
Revises: 033_ui_agent_owners
"""

import sqlalchemy as sa

from alembic import op

revision = "034_ui_dreams"
down_revision = "033_ui_agent_owners"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memoryhub_ui_dream_settings",
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("schedule", sa.String(16), nullable=False, server_default="off"),
        sa.Column("window", sa.String(5), nullable=False, server_default="02:00"),
        sa.Column("kinds", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("auto", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "user_id"),
        sa.CheckConstraint("schedule in ('nightly', 'weekly', 'off')", name="ck_memoryhub_ui_dream_settings_schedule"),
    )
    op.create_index("ix_memoryhub_ui_dream_settings_due", "memoryhub_ui_dream_settings", ["next_run_at"],
                    postgresql_where=sa.text("schedule <> 'off'"))
    op.create_table(
        "memoryhub_ui_dream_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("reviewed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("insights", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("counts", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memoryhub_ui_dream_runs_user", "memoryhub_ui_dream_runs", ["tenant_id", "user_id", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_memoryhub_ui_dream_runs_user", table_name="memoryhub_ui_dream_runs")
    op.drop_table("memoryhub_ui_dream_runs")
    op.drop_index("ix_memoryhub_ui_dream_settings_due", table_name="memoryhub_ui_dream_settings")
    op.drop_table("memoryhub_ui_dream_settings")
