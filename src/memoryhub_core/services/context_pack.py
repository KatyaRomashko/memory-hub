"""Render a context pack (#715): pinned memories, then saved-search results, within a token budget.

Used by the dashboard (preview and markdown download) and by MCP
(``memory(action="packs")``) so that an agent receives exactly what its owner
sees. The caller supplies ``readable``, its own read check, so each side keeps
its authorization model: the dashboard's human grants, MCP's token claims.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from memoryhub_core.models.knowledge import ContextPack
from memoryhub_core.models.memory import MemoryNode

Readable = Callable[[MemoryNode], Awaitable[bool]]
MAX_QUERIES = 10
MAX_PINNED = 100


def estimate_tokens(text: str) -> int:
    """About four characters per token; the dashboard says it is an estimate."""
    return max(1, math.ceil(len(text) / 4))


async def _current(session: AsyncSession, tenant_id: str, ids: list[uuid.UUID]) -> dict[uuid.UUID, MemoryNode]:
    if not ids:
        return {}
    rows = (await session.execute(select(MemoryNode).where(
        MemoryNode.id.in_(ids), MemoryNode.tenant_id == tenant_id,
        MemoryNode.is_current.is_(True), MemoryNode.deleted_at.is_(None), MemoryNode.status == "active",
    ))).scalars().all()
    return {node.id: node for node in rows}


async def render_pack(
    session: AsyncSession,
    pack: ContextPack,
    readable: Readable,
    embedding_service: Any | None,
    *,
    rendered_for: str,
) -> dict[str, Any]:
    """Return items in order, what did not fit, and the markdown an agent injects."""
    seen: set[uuid.UUID] = set()
    candidates: list[tuple[MemoryNode, str]] = []
    hidden = missing = 0

    pinned_ids: list[uuid.UUID] = []
    for raw in (pack.pinned or [])[:MAX_PINNED]:
        try:
            pinned_ids.append(uuid.UUID(str(raw)))
        except ValueError:
            missing += 1
    pinned = await _current(session, pack.tenant_id, pinned_ids)
    for memory_id in pinned_ids:
        node = pinned.get(memory_id)
        if node is None:
            missing += 1
        elif not await readable(node):
            hidden += 1
        elif node.id not in seen:
            seen.add(node.id)
            candidates.append((node, "pinned"))

    if embedding_service is not None:
        for query in (pack.queries or [])[:MAX_QUERIES]:
            text = str(query.get("query") or "").strip()
            if not text:
                continue
            limit = max(1, min(int(query.get("max_results") or 5), 50))
            vector = await embedding_service.embed(text)
            stmt = select(MemoryNode).where(
                MemoryNode.tenant_id == pack.tenant_id, MemoryNode.is_current.is_(True),
                MemoryNode.deleted_at.is_(None), MemoryNode.status == "active", MemoryNode.scope != "entity",
                MemoryNode.embedding.is_not(None),
                MemoryNode.weight >= float(query.get("weight_threshold") or 0),
            )
            if query.get("project"):
                stmt = stmt.where(MemoryNode.scope == "project", MemoryNode.scope_id == query["project"])
            rows = (await session.execute(stmt.order_by(MemoryNode.embedding.cosine_distance(vector))
                                          .limit(limit * 4))).scalars().all()
            found = 0
            for node in rows:
                if found >= limit:
                    break
                if node.id in seen or not await readable(node):
                    continue
                seen.add(node.id)
                found += 1
                candidates.append((node, f"search: {text}"))

    items, over_budget, used = [], [], 0
    for node, source in candidates:
        tokens = estimate_tokens(node.content)
        entry = {"id": str(node.id), "stub": node.stub, "scope": node.scope, "text": node.content,
                 "source": source, "tokens": tokens, "weight": node.weight}
        if used + tokens <= pack.token_budget:
            used += tokens
            items.append(entry)
        else:
            over_budget.append({k: entry[k] for k in ("id", "stub", "source", "tokens")})

    lines = [f"# {pack.name}"]
    if pack.description:
        lines += ["", pack.description]
    lines.append("")
    lines += [f"- {item['text']}  \n  _({item['scope']}, memory {item['id']})_" for item in items]
    return {
        "items": items, "tokens": used, "budget": pack.token_budget, "over_budget": over_budget,
        "hidden": hidden, "missing": missing, "markdown": "\n".join(lines) + "\n",
        "rendered_at": datetime.now(UTC).isoformat(), "as": rendered_for,
    }
