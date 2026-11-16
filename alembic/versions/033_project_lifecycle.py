"""Project lifecycle and policies: archive, soft delete, per-project policies.

Adds archived_at / archived_by (read-only projects), deleted_at / deleted_by
(soft delete; the name stays reserved) and a policies JSON object
(ttl_days, max_memories, default_weight) to projects. See
docs/design/projects-lifecycle.md.

Revision ID: 033_project_lifecycle
Revises: 032_persona_source_values
Part of #671.
"""

import sqlalchemy as sa

from alembic import op

revision = "033_project_lifecycle"
down_revision = "032_persona_source_values"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("projects", sa.Column("archived_by", sa.String(255), nullable=True))
    op.add_column("projects", sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("projects", sa.Column("deleted_by", sa.String(255), nullable=True))
    op.add_column(
        "projects",
        sa.Column("policies", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    op.drop_column("projects", "policies")
    op.drop_column("projects", "deleted_by")
    op.drop_column("projects", "deleted_at")
    op.drop_column("projects", "archived_by")
    op.drop_column("projects", "archived_at")
