"""Per-person preferences (#723) and the agent → owner binding they apply through."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from memoryhub_core.models.base import Base


class UserPreference(Base):
    """One row per person and tenant. ``prefs`` holds the validated document."""

    __tablename__ = "memoryhub_user_prefs"

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    prefs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Revision of the memory-behavior profile and its last few versions.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    notifications_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )


class AgentOwnerBinding(Base):
    """Read-only view of the dashboard's agent ownership (migration 033).

    The auth service and the dashboard write it; MCP reads it to apply the
    owner's preferences to the owner's agents.
    """

    __tablename__ = "memoryhub_ui_agent_owners"

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
