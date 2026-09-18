"""Where captured traffic goes.

* ``JsonlSink``     -- Phase 1: no MemoryHub needed; writes a local thread log.
* ``MemoryHubSink`` -- Phase 2/3: writes into MemoryHub conversation threads via
  the Python SDK and (optionally) triggers the existing extraction pipeline.

Both sinks expose the same async interface so the callback does not care.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from capture_core import Message, SessionInfo

log = logging.getLogger("memoryhub_capture")

# Upper bound when re-reading a thread after a proxy restart.
_SEED_LIMIT = 5000


@dataclass
class ThreadHandle:
    thread_id: str
    # Messages already stored when an existing thread was re-found; used to
    # seed delta tracking so the resent history is not appended twice.
    existing: list[Message] = field(default_factory=list)
    reused: bool = False


class Sink(Protocol):
    async def ensure_thread(self, session: SessionInfo) -> ThreadHandle: ...
    async def append(self, thread_id: str, msg: Message, *, actor_id: str | None, metadata: dict) -> None: ...
    async def extract(self, thread_id: str) -> dict | None: ...


class JsonlSink:
    """Append-only local log shaped like MemoryHub threads."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def _write(self, record: dict) -> None:
        async with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")

    async def ensure_thread(self, session: SessionInfo) -> ThreadHandle:
        thread_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"memoryhub-capture:{session.key}"))
        await self._write({"ts": time.time(), "event": "thread", "thread_id": thread_id, "session_key": session.key,
                           "session_source": session.source, "actor_id": session.actor_id})
        return ThreadHandle(thread_id)

    async def append(self, thread_id: str, msg: Message, *, actor_id: str | None, metadata: dict) -> None:
        await self._write({"ts": time.time(), "event": "append", "thread_id": thread_id, "role": msg.role,
                           "content": msg.content, "actor_id": actor_id, "metadata": metadata})

    async def extract(self, thread_id: str) -> dict | None:
        return None  # extraction needs MemoryHub


def _is_session_error(exc: BaseException) -> bool:
    """True for failures where the MCP session itself is gone.

    Only these are retried: retrying e.g. an append that failed after the
    server had already stored it would duplicate the message.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = ("session terminated", "session not found", "not connected", "connection",
               "closedresourceerror", "brokenresourceerror", "remoteprotocolerror", "readerror")
    return any(m in text for m in markers)


class MemoryHubSink:
    """Writes into MemoryHub via the SDK ``thread`` operations.

    The session key is stored as the thread's ``a2a_context_id`` so a
    restarted proxy can find the thread again instead of creating a new one.
    """

    def __init__(
        self,
        *,
        scope: str = "user",
        scope_id: str | None = None,
        extract_model: str | None = None,
        extract_model_url: str | None = None,
    ) -> None:
        self.scope = scope
        self.scope_id = scope_id
        self.extract_model = extract_model
        self.extract_model_url = extract_model_url
        self._client: Any = None
        self._connect_lock = asyncio.Lock()

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._connect_lock:
            if self._client is None:
                from memoryhub import MemoryHubClient  # imported lazily: optional dep

                client = MemoryHubClient.from_env(auto_discover_config=False)
                await client.__aenter__()
                self._client = client
                log.info("memoryhub_capture: connected to MemoryHub")
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:  # the session may already be dead
                pass
            self._client = None

    async def _call(self, fn):
        """Run ``fn(client)``; on a dropped MCP session reconnect once and retry.

        The proxy keeps one long-lived MCP session. It dies when the MCP pod
        restarts (e.g. after ``oc set env``), surfacing as
        ``McpError: Session terminated``.
        """
        client = await self._get_client()
        try:
            return await fn(client)
        except Exception as exc:
            if not _is_session_error(exc):
                raise
            log.warning("memoryhub_capture: MemoryHub session lost (%s); reconnecting", exc)
            await self.close()
            client = await self._get_client()
            return await fn(client)

    async def ensure_thread(self, session: SessionInfo) -> ThreadHandle:
        return await self._call(lambda client: self._ensure_thread(client, session))

    async def _ensure_thread(self, client: Any, session: SessionInfo) -> ThreadHandle:
        # Reuse an existing thread for this session (e.g. after a proxy restart).
        try:
            listing = await client.list_threads(scope=self.scope, limit=100)
            for t in listing.threads:
                if getattr(t, "a2a_context_id", None) == session.key:
                    got = await client.get_thread(t.id, limit=_SEED_LIMIT)
                    existing = [Message(m.role, m.content or "") for m in (got.messages or [])]
                    if got.has_more:
                        log.warning("memoryhub_capture: thread %s has more than %d messages; "
                                    "delta seeding is partial", t.id, _SEED_LIMIT)
                    return ThreadHandle(t.id, existing=existing, reused=True)
        except Exception as exc:
            if _is_session_error(exc):
                raise
            log.debug("memoryhub_capture: thread lookup failed: %s", exc)

        scope_id = session.project_id if self.scope == "project" and session.project_id else self.scope_id
        thread = await client.create_thread(
            self.scope,
            scope_id=scope_id,
            title=f"proxy-capture {session.key[:12]}",
            a2a_context_id=session.key,
            metadata={
                "source": "litellm-proxy",
                "poc": "WRIG-1482",
                "session_source": session.source,
                "observed_actor_id": session.actor_id,
                "observed_actor_source": session.actor_source,
            },
        )
        return ThreadHandle(thread.id)

    async def append(self, thread_id: str, msg: Message, *, actor_id: str | None, metadata: dict) -> None:
        # actor_id on append must be a MemoryHub identity the caller may act
        # as; the proxy's observed actor is kept in message metadata instead.
        await self._call(lambda client: client.append_message(
            thread_id, msg.role, msg.content, metadata={**metadata, "observed_actor_id": actor_id}))

    async def extract(self, thread_id: str) -> dict | None:
        result = await self._call(lambda client: client.extract_thread(
            thread_id, model=self.extract_model, model_url=self.extract_model_url))
        return result.model_dump()


class LocalSink:
    """Writes into the MemoryHub personal edition (``memoryhub-local``, SQLite).

    The local edition speaks MCP over stdio only, so there is no HTTP endpoint
    for the SDK. This sink therefore calls the local services directly and
    shares the same SQLite database the local MCP server uses
    (``$XDG_DATA_HOME/memoryhub/memoryhub.db``, default ``~/.local/share/...``).

    Extraction runs in this process through the same pipeline
    ``memoryhub dream`` uses, with an OpenAI-compatible endpoint supplied by
    the operator (Ollama, a hosted model, or the LiteLLM proxy itself).
    """

    def __init__(
        self,
        *,
        scope: str = "user",
        extract_model: str | None = None,
        extract_model_url: str | None = None,
        extract_api_key: str | None = None,
    ) -> None:
        self.scope = scope
        self.extract_model = extract_model or "local-extractor"
        self.extract_model_url = extract_model_url
        self.extract_api_key = extract_api_key
        self._state: Any = None
        self._init_lock = asyncio.Lock()

    async def _get_state(self) -> Any:
        if self._state is not None:
            return self._state
        async with self._init_lock:
            if self._state is None:
                from memoryhub_local.startup import initialize_backend

                self._state = await initialize_backend(quiet=True)
                log.info("memoryhub_capture: local MemoryHub backend ready")
        return self._state

    async def ensure_thread(self, session: SessionInfo) -> ThreadHandle:
        from sqlalchemy import select

        from memoryhub_local.identity import TENANT_ID
        from memoryhub_local.models.conversation import ConversationMessage, ConversationThread
        from memoryhub_local.services.thread import create_thread

        state = await self._get_state()
        async with state.session_factory() as db:
            found = (await db.execute(
                select(ConversationThread).where(
                    ConversationThread.tenant_id == TENANT_ID,
                    ConversationThread.a2a_context_id == session.key,
                    ConversationThread.status == "active",
                )
            )).scalars().first()
            if found is not None:
                msgs = (await db.execute(
                    select(ConversationMessage)
                    .where(ConversationMessage.thread_id == found.id)
                    .order_by(ConversationMessage.sequence_number)
                )).scalars().all()
                existing = [Message(m.role, m.content or "") for m in msgs]
                return ThreadHandle(str(found.id), existing=existing, reused=True)

            created = await create_thread(
                db,
                self.scope,
                title=f"proxy-capture {session.key[:12]}",
                metadata={
                    "source": "litellm-proxy",
                    "poc": "WRIG-1482",
                    "session_key": session.key,
                    "session_source": session.source,
                    "observed_actor_id": session.actor_id,
                    "observed_actor_source": session.actor_source,
                },
            )
            # create_thread has no a2a_context_id parameter; set it directly so a
            # restarted proxy can find this thread again.
            thread = await db.get(ConversationThread, uuid.UUID(created["id"]))
            thread.a2a_context_id = session.key
            await db.commit()
            return ThreadHandle(created["id"])

    async def append(self, thread_id: str, msg: Message, *, actor_id: str | None, metadata: dict) -> None:
        from memoryhub_local.services.thread import append_message

        state = await self._get_state()
        async with state.session_factory() as db:
            await append_message(
                db, thread_id, msg.role, msg.content,
                metadata={**metadata, "observed_actor_id": actor_id},
            )

    async def extract(self, thread_id: str) -> dict | None:
        from memoryhub_local.services.extraction import extract_from_thread, make_http_llm_fn

        if not self.extract_model_url:
            raise RuntimeError(
                "Local extraction needs MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL "
                "(any OpenAI-compatible endpoint, e.g. http://localhost:11434/v1 for Ollama)"
            )
        state = await self._get_state()
        llm_fn = make_http_llm_fn(self.extract_model, self.extract_model_url, self.extract_api_key)
        async with state.session_factory() as db:
            return await extract_from_thread(
                db, thread_id,
                llm_fn=llm_fn,
                embedding_service=state.embedding_service,
                recall_backend=state.recall_backend,
                extraction_model=self.extract_model,
            )


def sink_from_env() -> Sink:
    kind = os.environ.get("MEMORYHUB_CAPTURE_SINK", "jsonl").lower()
    if kind == "memoryhub":
        return MemoryHubSink(
            scope=os.environ.get("MEMORYHUB_CAPTURE_SCOPE", "user"),
            scope_id=os.environ.get("MEMORYHUB_CAPTURE_SCOPE_ID") or None,
            extract_model=os.environ.get("MEMORYHUB_CAPTURE_EXTRACT_MODEL") or None,
            extract_model_url=os.environ.get("MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL") or None,
        )
    if kind == "local":
        return LocalSink(
            scope=os.environ.get("MEMORYHUB_CAPTURE_SCOPE", "user"),
            extract_model=os.environ.get("MEMORYHUB_CAPTURE_EXTRACT_MODEL") or None,
            extract_model_url=os.environ.get("MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL") or None,
            extract_api_key=os.environ.get("MEMORYHUB_CAPTURE_EXTRACT_API_KEY") or None,
        )
    if kind == "jsonl":
        return JsonlSink(os.environ.get("MEMORYHUB_CAPTURE_THREADS_LOG", "./out/threads.jsonl"))
    raise ValueError(f"Unknown MEMORYHUB_CAPTURE_SINK={kind!r} (expected jsonl|local|memoryhub)")
