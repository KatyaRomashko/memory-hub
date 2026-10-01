"""LiteLLM proxy callback: implicit memory capture for MemoryHub (WRIG-1482 PoC).

Registered in ``config.yaml``::

    litellm_settings:
      callbacks: memoryhub_capture.proxy_handler_instance

For every successful LLM call the proxy makes, this callback:

1. normalizes the request messages + response (OpenAI or Anthropic shape);
2. derives a session key (header > LiteLLM session id > fingerprint);
3. appends only the *new* messages to a MemoryHub conversation thread
   (chat APIs resend the whole history on every call);
4. every ``MEMORYHUB_CAPTURE_EXTRACT_EVERY`` user turns, triggers the
   existing MemoryHub extraction pipeline on that thread -- unless the agent
   saved that turn itself through its own MemoryHub tool, which the proxy
   sees as a ``tool_use`` block in the traffic (``MEMORYHUB_CAPTURE_WHEN_AGENT_WRITES``);
5. writes an ``Observation`` line describing what the proxy could see.

The agent is never told about MemoryHub: no MCP tool, no instructions,
no hooks. The only client-side change is the LLM base URL.

The callback must never break inference, so every failure is logged and
swallowed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from dataclasses import replace
from pathlib import Path

# LiteLLM loads this file by path, not as a package; make siblings importable.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from litellm.integrations.custom_logger import CustomLogger  # noqa: E402

from capture_core import (  # noqa: E402
    DEFAULT_MAX_MESSAGE_BYTES,
    DEFAULT_WRITE_TOOLS,
    Observation,
    SessionState,
    build_observation,
    commit_delta,
    compute_delta,
    derive_session,
    find_agent_writes,
    normalize_messages,
    redact_messages,
    response_messages,
    seed_state,
    should_skip,
    transcript,
)
from redact import redactor_from_env  # noqa: E402
from sinks import Sink, sink_from_env  # noqa: E402

log = logging.getLogger("memoryhub_capture")

# LiteLLM leaves the root logger at WARNING, so these lines would be dropped and
# the proxy terminal would say nothing about capture. Attach our own handler
# instead of raising LiteLLM's own log level, which buries them in its noise.
if not log.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
    log.addHandler(_handler)
    log.propagate = False
log.setLevel(os.environ.get("MEMORYHUB_CAPTURE_LOG_LEVEL", "INFO").upper())


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    return default if val is None else val.strip().lower() in ("1", "true", "yes", "on")


class MemoryHubCaptureLogger(CustomLogger):
    def __init__(self, sink: Sink | None = None) -> None:
        super().__init__()
        self.sink = sink or sink_from_env()
        # Behaviour flags are re-read per call (see _refresh): LiteLLM imports
        # this module at proxy start, which may happen before the operator's
        # environment is fully set.
        self.enabled = True
        self.capture_tools = False
        self.extract_every = 0
        self.max_message_bytes = DEFAULT_MAX_MESSAGE_BYTES
        self.ignore_models: re.Pattern | None = None
        self._refresh()
        self.observations_path = Path(
            os.environ.get("MEMORYHUB_CAPTURE_OBSERVATIONS", "./out/observations.jsonl")
        )
        log.info(
            "memoryhub_capture: ready (sink=%s extract_every=%d tools=%s max_bytes=%d "
            "when_agent_writes=%s redact=%s)",
            os.environ.get("MEMORYHUB_CAPTURE_SINK", "jsonl"), self.extract_every,
            self.capture_tools, self.max_message_bytes, self.agent_write_policy,
            "on" if self.redactor.enabled else "OFF",
        )
        self.observations_path.parent.mkdir(parents=True, exist_ok=True)
        self.sessions: dict[str, SessionState] = {}
        # Two locks per session, deliberately separate. The capture lock guards
        # delta state and is held for milliseconds. The extraction lock serialises
        # extraction passes, which take tens of seconds. Sharing one lock means a
        # running extraction blocks the next call's callback until LiteLLM's
        # logging worker cancels it at 20s -- and that turn is then lost.
        self._locks: dict[str, asyncio.Lock] = {}
        self._extract_locks: dict[str, asyncio.Lock] = {}
        # Never wait for the capture lock longer than the logging worker will wait
        # for us; losing one observation beats losing the turn.
        self.lock_timeout = float(os.environ.get("MEMORYHUB_CAPTURE_LOCK_TIMEOUT", "8"))
        self._obs_lock = asyncio.Lock()
        # Extraction runs detached (see _extract_later); keep references so the
        # tasks are not garbage-collected mid-flight.
        self._extract_tasks: set[asyncio.Task] = set()

    def _refresh(self) -> None:
        """Re-read behaviour flags from the environment."""
        self.enabled = _env_bool("MEMORYHUB_CAPTURE_ENABLED", True)
        self.capture_tools = _env_bool("MEMORYHUB_CAPTURE_TOOLS", False)
        self.extract_every = int(os.environ.get("MEMORYHUB_CAPTURE_EXTRACT_EVERY", "0") or 0)
        self.max_message_bytes = int(
            os.environ.get("MEMORYHUB_CAPTURE_MAX_MESSAGE_BYTES", str(DEFAULT_MAX_MESSAGE_BYTES)) or 0
        )
        pattern = os.environ.get("MEMORYHUB_CAPTURE_IGNORE_MODELS", "")
        self.ignore_models = re.compile(pattern) if pattern else None
        # Credentials are stripped before anything is stored or hashed.
        # Rebuilt per call so an operator can change the rules without a
        # proxy restart (MEMORYHUB_CAPTURE_REDACT / _REDACT_EXTRA).
        self.redactor = redactor_from_env()
        # Hybrid policy: what to do with turns the agent already saved itself
        # (detected from tool_use traffic, see capture_core.find_agent_writes).
        #   extract      -- ignore it, capture and extract as usual
        #   defer        -- capture the turn, but leave extraction to a later
        #                   dreaming pass (default: the agent is the fast path,
        #                   dreaming is the safety net)
        #   skip-thread  -- do not even store those messages
        self.agent_write_policy = os.environ.get("MEMORYHUB_CAPTURE_WHEN_AGENT_WRITES", "defer").lower()
        self.write_tools = re.compile(
            os.environ.get("MEMORYHUB_CAPTURE_WRITE_TOOLS", DEFAULT_WRITE_TOOLS), re.I
        )

    # ── LiteLLM hooks ────────────────────────────────────────────────────

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._refresh()
        if not self.enabled:
            return
        try:
            await self._capture(kwargs)
        except Exception:  # never break inference
            log.exception("memoryhub_capture: capture failed")

    # ── core ─────────────────────────────────────────────────────────────

    async def _capture(self, kwargs: dict) -> None:
        slo = kwargs.get("standard_logging_object") or {}
        request_msgs = normalize_messages(slo.get("messages") or kwargs.get("messages"))
        full = transcript(request_msgs + response_messages(slo.get("response")), self.max_message_bytes)
        # Redact BEFORE the session key is derived: the fingerprint hashes the
        # first user message, and a secret must not survive even in a hash input.
        full, redactions = redact_messages(full, self.redactor)
        session = derive_session(slo, full, kwargs)

        skip = should_skip(slo, full, ignore_models=self.ignore_models)
        if skip:
            skipped_obs = build_observation(slo, session, full, None, skipped=skip)
            skipped_obs.redactions = redactions
            await self._observe(skipped_obs)
            return

        lock = self._locks.setdefault(session.key, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), timeout=self.lock_timeout)
        except asyncio.TimeoutError:
            obs = build_observation(slo, session, full, None, skipped="capture_lock_busy")
            obs.redactions = redactions
            obs.error = f"capture lock held longer than {self.lock_timeout}s"
            log.warning("memoryhub_capture: capture lock busy for %s; this turn is not stored",
                        session.key[:12])
            await self._observe(obs)
            return
        try:
            state = self.sessions.setdefault(session.key, SessionState())
            obs = build_observation(slo, session, full, compute_delta(state, full))
            obs.redactions = redactions
            try:
                if state.thread_id is None:
                    handle = await self.sink.ensure_thread(session)
                    state.thread_id = handle.thread_id
                    if handle.reused:
                        # proxy restarted: don't re-append what the thread already has
                        seed_state(state, handle.existing)
                obs.thread_id = state.thread_id
                delta = compute_delta(state, full)
                obs.new_messages = len(delta.new)
                obs.new_roles = [m.role for m in delta.new]
                obs.history_rewritten = delta.history_rewritten

                # Did the agent save anything itself in these new messages?
                writes = find_agent_writes(delta.new, self.write_tools)
                obs.agent_writes = len(writes)
                obs.agent_memory_ids = [w["memory_id"] for w in writes if w["memory_id"]]
                state.agent_writes += len(writes)
                state.agent_memory_ids.extend(obs.agent_memory_ids)

                meta = {
                    "source": "litellm-proxy",
                    "call_id": obs.call_id,
                    "model": obs.model,
                    "session_source": session.source,
                }
                if delta.history_rewritten:
                    meta["after_history_rewrite"] = True
                if writes:
                    # provenance for the memories the agent wrote by itself:
                    # they carry no thread link of their own
                    meta["agent_wrote_memory"] = True
                    if obs.agent_memory_ids:
                        meta["agent_memory_ids"] = obs.agent_memory_ids

                if writes and self.agent_write_policy == "skip-thread":
                    # the agent covered this turn; do not store it at all
                    obs.extract_skipped = "agent_wrote:thread-skipped"
                    commit_delta(state, full)
                    await self._observe(obs)
                    return
                for i, msg in enumerate(delta.new, start=delta.prefix_len + 1):
                    if not (msg.role in ("tool_call", "tool_result") and not self.capture_tools):
                        await self.sink.append(state.thread_id, msg, actor_id=session.actor_id, metadata=meta)
                        obs.appended += 1
                        if msg.role == "user":
                            state.user_turns += 1
                    # commit per message: a failure mid-way must not cause the
                    # already-stored messages to be appended again next time
                    commit_delta(state, full[:i])

                extraction_due = self.extract_every and state.user_turns >= self.extract_every
                if extraction_due and state.agent_writes and self.agent_write_policy in ("defer", "skip-thread"):
                    # The agent is already writing memories for this session.
                    # Keep the transcript, skip the extra LLM pass, and leave
                    # these messages for a later dreaming run, which will see
                    # the agent's memories and deduplicate against them.
                    obs.extract_skipped = f"agent_wrote:{state.agent_writes}"
                    extraction_due = False

                if extraction_due:
                    state.user_turns = 0
                    state.agent_writes = 0
                    state.agent_memory_ids.clear()
                    # Extraction is a second LLM call and routinely outlives
                    # LiteLLM's logging worker, which wraps every callback in
                    # asyncio.wait_for(timeout=LOGGING_WORKER_MAX_TIME_PER_COROUTINE,
                    # 20s by default) and cancels it. A cancellation lands in the
                    # middle of the pipeline: MemoryHub commits each memory as it
                    # is created but only commits the extraction cursor at the very
                    # end, so the memories survive and the cursor does not -- the
                    # same messages are then extracted again, as duplicates.
                    # Detaching the pass keeps the callback itself short and takes
                    # extraction out of that timeout entirely.
                    obs.extract_skipped = "scheduled in background"
                    task = asyncio.create_task(self._extract_later(session.key, state, obs))
                    self._extract_tasks.add(task)
                    task.add_done_callback(self._extract_tasks.discard)
            except Exception as exc:
                obs.error = f"{type(exc).__name__}: {exc}"
                log.warning("memoryhub_capture: sink error for %s: %s", session.key, exc)
        finally:
            lock.release()
        await self._observe(obs)

    async def drain(self) -> None:
        """Wait for extraction passes still running in the background."""
        while self._extract_tasks:
            await asyncio.gather(*list(self._extract_tasks), return_exceptions=True)

    async def _extract_later(self, session_key: str, state: SessionState, src: Observation) -> None:
        """Run the extraction pass outside the proxy's logging timeout."""
        # a fresh timestamp: this line is written when the pass FINISHES, not
        # when the call that scheduled it came in
        obs = replace(src, event="extraction", ts=time.time(), new_messages=0,
                      new_roles=[], appended=0, extract_skipped=None, error=None)
        async with self._extract_locks.setdefault(session_key, asyncio.Lock()):
            t0 = time.monotonic()
            try:
                obs.extraction = await self.sink.extract(state.thread_id)
            except Exception as exc:
                obs.error = f"{type(exc).__name__}: {exc}"
                log.warning("memoryhub_capture: extraction failed on thread %s: %s",
                            state.thread_id, exc)
            obs.extract_ms = (time.monotonic() - t0) * 1000
            obs.ts = time.time()          # when the pass FINISHED, not when it was queued
            if obs.extraction and obs.extraction.get("failures"):
                # MemoryHub advances the cursor past failed windows, so these
                # messages will not be retried automatically.
                log.warning(
                    "memoryhub_capture: extraction had %s failed window(s) on thread %s; "
                    "re-run with `verify.py reextract`",
                    obs.extraction["failures"], state.thread_id,
                )
            await self._observe(obs)

    async def _observe(self, obs: Observation) -> None:
        async with self._obs_lock:
            with self.observations_path.open("a", encoding="utf-8") as fh:
                fh.write(obs.to_json() + "\n")
        if obs.event == "extraction":
            e = obs.extraction or {}
            log.info("memoryhub_capture: extraction on %s -- %s memories, %s window(s), "
                     "cursor %s, %.0f ms%s",
                     str(obs.thread_id)[:8], e.get("extracted_count"), e.get("windows_processed"),
                     e.get("cursor"), obs.extract_ms or 0,
                     f" ERROR {obs.error}" if obs.error else "")
            shadow = (e.get("shadow") or {})
            if shadow:
                sr = shadow.get("result")
                if sr is None:
                    log.warning("memoryhub_capture:   shadow %s -- NO RESULT (see warning above)",
                                shadow.get("sink"))
                else:
                    log.info("memoryhub_capture:   shadow %s -- %s new, %s total, %.0f ms",
                             shadow.get("sink"), sr.get("extracted_count"),
                             sr.get("total_memories"), sr.get("extract_ms") or 0)
            return
        log.info(
            "memoryhub_capture: session=%s(%s) new=%d appended=%d skipped=%s rewritten=%s "
            "agent_writes=%d extract_skipped=%s%s",
            obs.session_key[:12], obs.session_source, obs.new_messages, obs.appended,
            obs.skipped, obs.history_rewritten, obs.agent_writes, obs.extract_skipped,
            f" REDACTED={sum(obs.redactions.values())}{sorted(obs.redactions)}" if obs.redactions else "",
        )


proxy_handler_instance = MemoryHubCaptureLogger()
