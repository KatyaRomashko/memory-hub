"""Per-person preferences (#723): defaults, validation, and how they apply to agents.

The dashboard edits them (``GET/PATCH /api/me/prefs``); MCP applies them when
one of the person's agents writes:

- ``write.default_weight`` is the weight of a memory written without one.
- ``behavior.custom_exclude`` topics are never stored: a write whose text
  mentions one is refused and audited (the matched text is not recorded).

- ``dreaming.enabled`` off: conversation extraction skips the person's
  threads (``memoryhub_core.services.dreaming.extract_from_thread``).

The rest of the memory-behavior profile (what to look for, when, whether to
confirm) is guidance for agent hosts, stored and versioned here.
"""

from __future__ import annotations

import copy
import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from memoryhub_core.models.user_prefs import AgentOwnerBinding, UserPreference

PROFILE_SCHEMA = "memoryhub.memory-behavior/v1"
CATEGORIES = ["preferences", "project_decisions", "procedures", "facts"]
EXCLUDES = ["credentials", "personal_data", "health", "customer_data"]
CHOICES = {
    "confirmation": ["always", "sensitive", "never"],
    "cadence": ["every_turn", "session_end", "explicit"],
    "update": ["supersede", "branch", "ask"],
}
TEMPLATES: dict[str, dict] = {
    "balanced": {"template": "balanced", "include": list(CATEGORIES), "exclude": list(EXCLUDES),
                 "confirmation": "sensitive", "cadence": "session_end", "update": "supersede"},
    "conservative": {"template": "conservative", "include": ["preferences", "project_decisions"],
                     "exclude": list(EXCLUDES), "confirmation": "always", "cadence": "explicit", "update": "ask"},
    "proactive": {"template": "proactive", "include": list(CATEGORIES), "exclude": ["credentials"],
                  "confirmation": "never", "cadence": "every_turn", "update": "supersede"},
}
HISTORY_KEEP = 5

DEFAULTS: dict = {
    "write": {"default_scope": "user", "default_weight": 0.7},
    "dreaming": {"enabled": True, "fade_days": 60},
    "onboarding_dismissed": False,
    "behavior": {**TEMPLATES["balanced"], "custom_exclude": []},
}


class WritePrefs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_scope: Literal["user", "project"] = "user"
    default_weight: float = Field(default=0.7, ge=0.0, le=1.0)


class DreamingPrefs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    fade_days: int = Field(default=60, ge=14, le=365)


class BehaviorProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: Literal["balanced", "conservative", "proactive", "custom"] = "balanced"
    include: list[Literal["preferences", "project_decisions", "procedures", "facts"]] = Field(default_factory=list)
    exclude: list[Literal["credentials", "personal_data", "health", "customer_data"]] = Field(default_factory=list)
    custom_exclude: list[str] = Field(default_factory=list, max_length=20)
    confirmation: Literal["always", "sensitive", "never"] = "sensitive"
    cadence: Literal["every_turn", "session_end", "explicit"] = "session_end"
    update: Literal["supersede", "branch", "ask"] = "supersede"

    @field_validator("include", "exclude")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @field_validator("custom_exclude")
    @classmethod
    def _topics(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for topic in value:
            topic = " ".join(topic.split())
            if not topic:
                continue
            if len(topic) > 200:
                raise ValueError("a topic is at most 200 characters")
            if topic.lower() not in (t.lower() for t in cleaned):
                cleaned.append(topic)
        return cleaned


class PrefsPatch(BaseModel):
    """A partial update; every section is optional and replaced as a whole."""

    model_config = ConfigDict(extra="forbid")
    write: WritePrefs | None = None
    dreaming: DreamingPrefs | None = None
    onboarding_dismissed: bool | None = None
    behavior: BehaviorProfile | None = None


def effective(stored: dict | None) -> dict:
    """The stored document over the defaults, section by section."""
    prefs = copy.deepcopy(DEFAULTS)
    for key, value in (stored or {}).items():
        if key in prefs and isinstance(prefs[key], dict) and isinstance(value, dict):
            prefs[key].update(value)
        elif key in prefs:
            prefs[key] = value
    return prefs


async def load(session: AsyncSession, tenant_id: str, user_id: str, *, lock: bool = False) -> UserPreference | None:
    stmt = select(UserPreference).where(UserPreference.tenant_id == tenant_id, UserPreference.user_id == user_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def apply_patch(session: AsyncSession, tenant_id: str, user_id: str, patch: PrefsPatch) -> UserPreference:
    """Store a validated patch. A changed behavior profile gets a new revision."""
    row = await load(session, tenant_id, user_id, lock=True)
    if row is None:
        row = UserPreference(tenant_id=tenant_id, user_id=user_id, prefs={}, revision=0, history=[])
        session.add(row)
    current = effective(row.prefs)
    updates = patch.model_dump(exclude_none=True)
    if "behavior" in updates and updates["behavior"] != {k: current["behavior"].get(k) for k in updates["behavior"]}:
        if row.revision:
            previous = {**current["behavior"], "revision": row.revision,
                        "updated_at": current.get("behavior_updated_at")}
            row.history = ([previous] + list(row.history or []))[:HISTORY_KEEP]
        row.revision = (row.revision or 0) + 1
        updates["behavior_updated_at"] = datetime.now(UTC).isoformat()
    row.prefs = {**(row.prefs or {}), **updates}
    return row


async def owner_of(session: AsyncSession, tenant_id: str, identity: str) -> str:
    """The person an identity acts for: an agent's owner, or the identity itself."""
    owner = (await session.execute(select(AgentOwnerBinding.owner).where(
        AgentOwnerBinding.tenant_id == tenant_id, AgentOwnerBinding.client_id == identity,
    ))).scalar_one_or_none()
    return owner or identity


async def prefs_for_identity(session: AsyncSession, tenant_id: str, identity: str) -> tuple[str, dict]:
    owner = await owner_of(session, tenant_id, identity)
    row = await load(session, tenant_id, owner)
    return owner, effective(row.prefs if row else None)


def excluded_topic(content: str, prefs: dict) -> str | None:
    """The first never-store topic the content mentions (whole words, any case)."""
    for topic in prefs.get("behavior", {}).get("custom_exclude", []) or []:
        words = [re.escape(word) for word in topic.split()]
        if words and re.search(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", content, re.IGNORECASE):
            return topic
    return None
