"""Where captured traffic goes.

* **JsonlSink**     -- Phase 1: no MemoryHub needed; writes a local thread log.
* **LocalSink**     -- personal edition: writes SQLite via memoryhub-local services.
* **MemoryHubSink** -- cluster edition: SDK ``thread`` ops.
* **HindsightSink**  -- a *different memory system* behind the same contract.
* **ShadowSink**     -- fans one conversation out to two of the above at once.

All sinks expose the same async interface so the callback does not care. That
is the claim the gateway makes -- the transport layer is memory-system
agnostic -- and ``HindsightSink`` plus ``ShadowSink`` are how it is tested
rather than asserted: the same conversation goes into two stores, and what
each one chose to remember can be compared afterwards.
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

    def _refresh_extract_env(self) -> None:
        self.extract_model = os.environ.get("MEMORYHUB_CAPTURE_EXTRACT_MODEL") or self.extract_model
        self.extract_model_url = os.environ.get("MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL") or self.extract_model_url
        self.extract_api_key = os.environ.get("MEMORYHUB_CAPTURE_EXTRACT_API_KEY") or self.extract_api_key

    async def extract(self, thread_id: str) -> dict | None:
        from memoryhub_local.services.extraction import extract_from_thread, make_http_llm_fn

        self._refresh_extract_env()
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


class HindsightSink:
    """Adapter for Hindsight (github.com/vectorize-io/hindsight), API 0.10.x.

    Hindsight models memory very differently from MemoryHub, which is exactly
    why it is the useful second provider:

    * there is **no thread or message resource**. A whole conversation is
      retained as ONE item, and re-retaining with the same ``document_id``
      upserts it (the old version is deleted and reprocessed);
    * **extraction is Hindsight's own job** and runs inside ``retain``. There
      is no endpoint that writes a pre-formed memory row;
    * the isolation boundary is a **bank**, which springs into existence on
      first write. Nothing has to be created up front.

    So the Sink contract maps on as:

    ==================  ====================================================
    ``ensure_thread``   pick the bank and document id; no network call
    ``append``          buffer the message locally; no network call
    ``extract``         retain the whole buffered conversation -- which is
                        when Hindsight extracts -- then count what it kept
    ==================  ====================================================

    ``extract`` is driven by the same ``EXTRACT_EVERY`` cadence as MemoryHub's
    dreaming pass, so both systems are asked to think at the same moments
    about the same text. Retain is synchronous and calls an LLM, so it is slow
    for the same reason dreaming is.

    Restarting the proxy loses the buffer; the next retain then carries only
    the messages seen since. Hindsight upserts by ``document_id``, so that
    *shrinks* the document rather than duplicating it. Good enough for a demo,
    wrong for production -- a real adapter would rebuild the buffer from the
    primary store or keep it on disk.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        bank: str | None = None,
        api_key: str | None = None,
        timeout: float = 300.0,
        context: str | None = None,
    ) -> None:
        self.base_url = (base_url or "http://localhost:8888").rstrip("/")
        self.bank = bank or "memoryhub-proxy-demo"
        self.api_key = api_key
        self.timeout = timeout
        self.context = context or (
            "A conversation between a user and an AI assistant, captured "
            "passively at an LLM gateway. Remember what the user stated about "
            "their project, decisions and preferences."
        )
        self._buffers: dict[str, list[Message]] = {}
        self._counts: dict[str, int] = {}
        self._client: Any = None
        self._lock = asyncio.Lock()

    # -- transport ------------------------------------------------------

    async def _http(self) -> Any:
        if self._client is None:
            import httpx  # litellm already depends on it

            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout,
                                             headers=headers)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _path(self, suffix: str) -> str:
        # `default` is a literal in the OSS build, not a tenant variable.
        return f"/v1/default/banks/{self.bank}{suffix}"

    async def health(self) -> dict:
        client = await self._http()
        r = await client.get("/health/live")
        r.raise_for_status()
        return {"live": r.json(), "base_url": self.base_url, "bank": self.bank}

    # -- Sink contract --------------------------------------------------

    async def ensure_thread(self, session: SessionInfo) -> ThreadHandle:
        # The document id is the session key: stable across proxy restarts,
        # and upsert semantics mean re-retaining it is safe.
        doc_id = f"proxy-{session.key}"[:200]
        self._buffers.setdefault(doc_id, [])
        log.info("memoryhub_capture: hindsight bank=%s document=%s", self.bank, doc_id)
        return ThreadHandle(doc_id)

    async def append(self, thread_id: str, msg: Message, *, actor_id: str | None, metadata: dict) -> None:
        async with self._lock:
            self._buffers.setdefault(thread_id, []).append(msg)

    def _render(self, messages: list[Message]) -> str:
        """The conversation as one document.

        Hindsight's own guidance: retain a full conversation as a single item
        whose text "clearly conveys who said what and when". The sequence
        numbers mirror what MemoryHub's extraction window shows, so the two
        systems are reading the same thing in the same order.
        """
        lines = []
        for i, m in enumerate(messages, start=1):
            lines.append(f"{m.role} (message {i}): {m.content}")
        return "\n".join(lines)

    async def extract(self, thread_id: str) -> dict | None:
        async with self._lock:
            messages = list(self._buffers.get(thread_id, []))
        if not messages:
            return {"system": "hindsight", "extracted_count": 0, "windows_processed": 0,
                    "cursor": 0, "failures": 0, "note": "nothing buffered"}

        client = await self._http()
        body = {
            "items": [{
                "content": self._render(messages),
                "document_id": thread_id,
                "context": self.context,
                "metadata": {"source": "litellm-proxy", "poc": "WRIG-1482"},
            }],
            "async": False,
        }
        before = self._counts.get(thread_id, 0)
        resp = await client.post(self._path("/memories"), json=body)
        resp.raise_for_status()
        retained = resp.json()

        after, failures = before, 0
        try:
            listed = await client.get(self._path("/memories/list"),
                                      params={"document_id": thread_id, "limit": 200})
            listed.raise_for_status()
            payload = listed.json()
            items = payload.get("items") or []
            after = payload.get("total", len(items))
        except Exception as exc:  # listing is telemetry, not the write path
            failures = 1
            log.warning("memoryhub_capture: hindsight list failed: %s", exc)

        self._counts[thread_id] = after
        return {
            "system": "hindsight",
            "bank": self.bank,
            "document_id": thread_id,
            # shaped like MemoryHub's extraction result so one reader handles both
            "extracted_count": max(after - before, 0),
            "total_memories": after,
            "windows_processed": 1,
            "cursor": len(messages),
            "failures": failures,
            "retained": retained,
        }

    async def recall(self, query: str, *, limit: int = 5) -> list[dict]:
        """Read path, used by live/facts.py -- not part of the Sink contract."""
        client = await self._http()
        r = await client.post(self._path("/memories/recall"),
                              json={"query": query, "budget": "mid", "max_tokens": 2048})
        r.raise_for_status()
        return (r.json().get("results") or [])[:limit]


class ShadowSink:
    """Send one conversation into two memory systems at once.

    The point is control of variables. Running the same task twice against two
    memory systems does not compare them: memory changes the agent's answers,
    so by the second turn the two runs are different conversations. Fanning one
    live conversation out to both means identical input, identical sessions,
    identical model -- the only difference is what each system did with it.

    The primary decides everything the proxy does: its thread id is the one
    tracked, its errors are the ones raised. The shadow can never affect
    inference or the primary's state; its failures are logged and dropped.
    """

    def __init__(self, primary: Sink, shadow: Sink, *,
                 primary_name: str = "primary", shadow_name: str = "shadow") -> None:
        self.primary = primary
        self.shadow = shadow
        self.primary_name = primary_name
        self.shadow_name = shadow_name
        self._ids: dict[str, str] = {}   # primary thread id -> shadow thread id

    async def _quietly(self, coro, what: str):
        try:
            return await coro
        except Exception as exc:
            log.warning("memoryhub_capture: shadow (%s) %s failed: %s", self.shadow_name, what, exc)
            return None

    async def ensure_thread(self, session: SessionInfo) -> ThreadHandle:
        handle = await self.primary.ensure_thread(session)
        mirrored = await self._quietly(self.shadow.ensure_thread(session), "ensure_thread")
        if mirrored is not None:
            self._ids[handle.thread_id] = mirrored.thread_id
        return handle

    async def append(self, thread_id: str, msg: Message, *, actor_id: str | None, metadata: dict) -> None:
        await self.primary.append(thread_id, msg, actor_id=actor_id, metadata=metadata)
        shadow_id = self._ids.get(thread_id)
        if shadow_id:
            await self._quietly(
                self.shadow.append(shadow_id, msg, actor_id=actor_id,
                                   metadata={**metadata, "shadow_of": thread_id}),
                "append")

    async def extract(self, thread_id: str) -> dict | None:
        """Both systems think about the same text, in the same moment."""
        result = await self.primary.extract(thread_id)
        shadow_id = self._ids.get(thread_id)
        shadow_result = None
        if shadow_id:
            t0 = time.monotonic()
            shadow_result = await self._quietly(self.shadow.extract(shadow_id), "extract")
            if isinstance(shadow_result, dict):
                shadow_result["extract_ms"] = (time.monotonic() - t0) * 1000
        out = dict(result or {})
        out["shadow"] = {"sink": self.shadow_name, "result": shadow_result}
        return out


def _one_sink(kind: str) -> Sink:
    kind = (kind or "jsonl").lower()
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
    if kind == "hindsight":
        return HindsightSink(
            base_url=os.environ.get("MEMORYHUB_CAPTURE_HINDSIGHT_URL") or None,
            bank=os.environ.get("MEMORYHUB_CAPTURE_HINDSIGHT_BANK") or None,
            api_key=os.environ.get("MEMORYHUB_CAPTURE_HINDSIGHT_API_KEY") or None,
        )
    if kind == "jsonl":
        return JsonlSink(os.environ.get("MEMORYHUB_CAPTURE_THREADS_LOG", "./out/threads.jsonl"))
    raise ValueError(f"Unknown sink {kind!r} (expected jsonl|local|memoryhub|hindsight)")


def sink_from_env() -> Sink:
    """Build the sink, wrapping it in a shadow when a second one is configured.

        MEMORYHUB_CAPTURE_SINK=local            # who answers and is tracked
        MEMORYHUB_CAPTURE_SHADOW_SINK=hindsight # who also gets everything
    """
    primary_kind = os.environ.get("MEMORYHUB_CAPTURE_SINK", "jsonl")
    shadow_kind = (os.environ.get("MEMORYHUB_CAPTURE_SHADOW_SINK") or "").strip()
    primary = _one_sink(primary_kind)
    if not shadow_kind or shadow_kind.lower() == "none":
        return primary
    log.info("memoryhub_capture: shadow mode -- primary=%s shadow=%s",
             primary_kind.lower(), shadow_kind.lower())
    return ShadowSink(primary, _one_sink(shadow_kind),
                      primary_name=primary_kind.lower(), shadow_name=shadow_kind.lower())
