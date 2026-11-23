"""Teams for the dashboard: named groups of people, linked to projects.

Revision ID: 034_ui_teams
Revises: 033_ui_agent_owners
"""

import sqlalchemy as sa

from alembic import op

revision = "034_ui_teams"
down_revision = "033_ui_agent_owners"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memoryhub_ui_teams",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_memoryhub_ui_teams_tenant_name"),
    )
    op.create_table(
        "memoryhub_ui_team_members",
        sa.Column("team_id", sa.Uuid(), sa.ForeignKey("memoryhub_ui_teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="member"),
        sa.Column("added_by", sa.String(255), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("team_id", "user_id"),
        sa.CheckConstraint("role in ('lead', 'member')", name="ck_memoryhub_ui_team_members_role"),
    )
    op.create_index("ix_memoryhub_ui_team_members_user", "memoryhub_ui_team_members", ["user_id"])
    op.create_table(
        "memoryhub_ui_team_projects",
        sa.Column("team_id", sa.Uuid(), sa.ForeignKey("memoryhub_ui_teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.String(255), sa.ForeignKey("projects.name", ondelete="CASCADE"), nullable=False),
        sa.Column("linked_by", sa.String(255), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("team_id", "project_id"),
    )


def downgrade() -> None:
    op.drop_table("memoryhub_ui_team_projects")
    op.drop_index("ix_memoryhub_ui_team_members_user", table_name="memoryhub_ui_team_members")
    op.drop_table("memoryhub_ui_team_members")
    op.drop_table("memoryhub_ui_teams")
