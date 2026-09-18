"""Print the local thread id for a capture session key (used by run-poc-local.sh)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


async def main() -> None:
    from sqlalchemy import select

    from memoryhub_local.identity import TENANT_ID
    from memoryhub_local.models.conversation import ConversationThread

    from _local import open_backend
    state, _embed_kind = await open_backend()
    async with state.session_factory() as db:
        rows = (await db.execute(select(ConversationThread).where(
            ConversationThread.tenant_id == TENANT_ID))).scalars().all()
        wanted = sys.argv[1]
        for t in rows:
            if wanted == "--all":
                if (t.metadata_ or {}).get("source") == "litellm-proxy":
                    print(t.id)
            elif t.a2a_context_id == wanted:
                print(t.id)
                return
        if wanted == "--all":
            return
    print("", end="")


if __name__ == "__main__":
    asyncio.run(main())
