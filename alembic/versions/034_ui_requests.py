"""Dashboard requests: promotion and shared-knowledge requests (#719).

Revision ID: 034_ui_requests
Revises: 033_ui_agent_owners
"""

import sqlalchemy as sa

from alembic import op

revision = "034_ui_requests"
down_revision = "033_ui_agent_owners"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memoryhub_ui_requests",
        sa.Column("id", sa.String(16), nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("requester", sa.String(255), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("memory_version", sa.Integer(), nullable=False),
        sa.Column("snapshot_content", sa.Text(), nullable=False),
        sa.Column("from_scope", sa.String(32), nullable=False),
        sa.Column("from_scope_id", sa.String(255), nullable=True),
        sa.Column("target_scope", sa.String(32), nullable=False),
        sa.Column("target_scope_id", sa.String(255), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="open"),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("decided_by", sa.String(255), nullable=True),
        sa.Column("decided_by_role", sa.String(32), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_memory_id", sa.Uuid(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("state in ('open', 'decided', 'withdrawn')", name="ck_memoryhub_ui_requests_state"),
        sa.CheckConstraint("kind in ('promotion', 'shared_knowledge')", name="ck_memoryhub_ui_requests_kind"),
        sa.CheckConstraint("target_scope in ('project', 'organizational')", name="ck_memoryhub_ui_requests_target"),
    )
    op.create_index("ix_memoryhub_ui_requests_requester", "memoryhub_ui_requests", ["tenant_id", "requester"])
    op.create_index("ix_memoryhub_ui_requests_open", "memoryhub_ui_requests", ["tenant_id", "state", "target_scope"])
    # One open request per memory and kind.
    op.create_index(
        "uq_memoryhub_ui_requests_open_memory", "memoryhub_ui_requests", ["tenant_id", "memory_id", "kind"],
        unique=True, postgresql_where=sa.text("state = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("uq_memoryhub_ui_requests_open_memory", table_name="memoryhub_ui_requests")
    op.drop_index("ix_memoryhub_ui_requests_open", table_name="memoryhub_ui_requests")
    op.drop_index("ix_memoryhub_ui_requests_requester", table_name="memoryhub_ui_requests")
    op.drop_table("memoryhub_ui_requests")
