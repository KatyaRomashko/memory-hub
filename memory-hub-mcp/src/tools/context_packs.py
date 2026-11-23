"""Context packs for agents (#715): ``memory(action="packs")``.

A person attaches their context packs to their own agents in the dashboard.
The agent receives each attached pack rendered with *its own* read checks
(``memory_object_access``), so a pack never shows an agent a memory the agent
could not read directly. A pack stops being served when its owner no longer
owns the agent.
"""

import logging
from typing import Any

from fastmcp.exceptions import ToolError
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import JSONB
from src.core.authz import AuthenticationError, get_claims_from_context
from src.tools._audit_helpers import record_audit_event
from src.tools._deps import get_db_session, get_embedding_service, release_db_session
from src.tools._object_access import memory_object_access

from memoryhub_core.models.knowledge import ContextPack
from memoryhub_core.services.context_pack import render_pack

logger = logging.getLogger(__name__)

MAX_PACKS = 20


async def _agent_owner(session, tenant: str, client_id: str) -> str | None:
    if (await session.execute(text("SELECT to_regclass('memoryhub_ui_agent_owners')"))).scalar() is None:
        return None
    return (await session.execute(
        text("SELECT owner FROM memoryhub_ui_agent_owners WHERE tenant_id = :t AND client_id = :c"),
        {"t": tenant, "c": client_id},
    )).scalar_one_or_none()


async def context_packs(name: str | None = None, ctx=None) -> dict[str, Any]:
    """Return the context packs attached to the calling agent, rendered for it."""
    try:
        claims = get_claims_from_context()
    except AuthenticationError as exc:
        raise ToolError(str(exc)) from exc
    sub = claims["sub"]
    tenant = claims.get("tenant_id", "default")

    session, gen = await get_db_session()
    try:
        owner = await _agent_owner(session, tenant, sub)
        if owner is None:
            return {"packs": [], "total": 0, "note": "This agent has no owner in the dashboard, so no packs."}
        stmt = select(ContextPack).where(
            ContextPack.tenant_id == tenant, ContextPack.owner == owner,
            ContextPack.agents.cast(JSONB).contains([sub]),
        )
        if name:
            stmt = stmt.where(ContextPack.name == name)
        packs = (await session.execute(stmt.order_by(ContextPack.name).limit(MAX_PACKS))).scalars().all()

        async def readable(node) -> bool:
            can_read, _ = await memory_object_access(session, claims, node)
            return can_read

        try:
            embedding = get_embedding_service()
        except Exception:  # noqa: BLE001 - pinned memories still render without search
            logger.warning("Embedding service unavailable; packs render pinned memories only")
            embedding = None
        out = []
        for pack in packs:
            if sub not in (pack.agents or []):
                continue
            render = await render_pack(session, pack, readable, embedding, rendered_for=sub)
            out.append({"id": str(pack.id), "name": pack.name, "description": pack.description,
                        "owner": pack.owner, "tokens": render["tokens"], "budget": render["budget"],
                        "items": [{k: i[k] for k in ("id", "stub", "scope", "text", "source")}
                                  for i in render["items"]],
                        "omitted": len(render["over_budget"]) + render["hidden"] + render["missing"],
                        "markdown": render["markdown"]})
        if out:
            await record_audit_event(
                "packs.served", sub, sub, "user", owner, None, "allowed", tenant,
                metadata={"packs": [p["id"] for p in out]}, session=session,
            )
            await session.commit()
        return {"packs": out, "total": len(out)}
    finally:
        await release_db_session(gen)
