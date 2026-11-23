"""Knowledge: documents, their versions and memory links, and context packs (#715).

Revision ID: 034_knowledge
Revises: 033_ui_agent_owners
"""

import sqlalchemy as sa

from alembic import op

revision = "034_knowledge"
down_revision = "033_ui_agent_owners"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memoryhub_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("owner", sa.String(255), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("mime", sa.String(100), nullable=False, server_default="text/plain"),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("project", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memoryhub_documents_owner", "memoryhub_documents", ["tenant_id", "owner"])
    op.create_table(
        "memoryhub_document_versions",
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("memoryhub_documents.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("document_id", "version"),
    )
    op.create_table(
        "memoryhub_document_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("memoryhub_documents.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("start", sa.Integer(), nullable=False),
        sa.Column("end", sa.Integer(), nullable=False),
        sa.Column("passage", sa.Text(), nullable=False),
        sa.Column("doc_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("status in ('ok', 'moved', 'changed')", name="ck_memoryhub_document_links_status"),
    )
    op.create_index("ix_memoryhub_document_links_document", "memoryhub_document_links", ["document_id"])
    op.create_index("ix_memoryhub_document_links_memory", "memoryhub_document_links", ["memory_id"])
    op.create_table(
        "memoryhub_context_packs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("owner", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("pinned", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("queries", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("token_budget", sa.Integer(), nullable=False, server_default="2000"),
        sa.Column("visibility", sa.String(16), nullable=False, server_default="private"),
        sa.Column("project", sa.String(255), nullable=True),
        sa.Column("agents", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("last_render", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("visibility in ('private', 'project')", name="ck_memoryhub_context_packs_visibility"),
    )
    op.create_index("ix_memoryhub_context_packs_owner", "memoryhub_context_packs", ["tenant_id", "owner"])


def downgrade() -> None:
    op.drop_index("ix_memoryhub_context_packs_owner", table_name="memoryhub_context_packs")
    op.drop_table("memoryhub_context_packs")
    op.drop_index("ix_memoryhub_document_links_memory", table_name="memoryhub_document_links")
    op.drop_index("ix_memoryhub_document_links_document", table_name="memoryhub_document_links")
    op.drop_table("memoryhub_document_links")
    op.drop_table("memoryhub_document_versions")
    op.drop_index("ix_memoryhub_documents_owner", table_name="memoryhub_documents")
    op.drop_table("memoryhub_documents")
