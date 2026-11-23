"""Knowledge (#715): documents memories are written from, and context packs for agents."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from memoryhub_core.models.base import Base


class Document(Base):
    """A text document a person uploaded. ``body`` is the current version."""

    __tablename__ = "memoryhub_documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    mime: Mapped[str] = mapped_column(String(100), nullable=False, default="text/plain")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Members of this project can read the document; only the owner changes it.
    project: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_memoryhub_documents_owner", "tenant_id", "owner"),)


class DocumentVersion(Base):
    """Earlier versions of a document, kept when a new one is uploaded."""

    __tablename__ = "memoryhub_document_versions"

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("memoryhub_documents.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DocumentLink(Base):
    """A memory written from a span of a document, and whether that span still holds."""

    __tablename__ = "memoryhub_document_links"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("memoryhub_documents.id", ondelete="CASCADE"), nullable=False)
    memory_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    start: Mapped[int] = mapped_column(Integer, nullable=False)
    end: Mapped[int] = mapped_column(Integer, nullable=False)
    # The span's text when the memory was written, to find it again in later versions.
    passage: Mapped[str] = mapped_column(Text, nullable=False)
    doc_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")  # ok | moved | changed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_memoryhub_document_links_document", "document_id"),
        Index("ix_memoryhub_document_links_memory", "memory_id"),
    )


class ContextPack(Base):
    """Pinned memories and saved searches, rendered within a token budget for agents."""

    __tablename__ = "memoryhub_context_packs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    pinned: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    queries: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=2000)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="private")  # private | project
    project: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Agent client ids (the owner's) that receive the pack through MCP.
    agents: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    last_render: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_memoryhub_context_packs_owner", "tenant_id", "owner"),)
