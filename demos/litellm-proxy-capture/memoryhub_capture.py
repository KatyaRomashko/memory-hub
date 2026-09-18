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
   existing MemoryHub extraction pipeline on that thread;
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
from pathlib import Path

# LiteLLM loads this file by path, not as a package; make siblings importable.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from litellm.integrations.custom_logger import CustomLogger  # noqa: E402

from capture_core import (  # noqa: E402
    DEFAULT_MAX_MESSAGE_BYTES,
    Observation,
    SessionState,
    build_observation,
    commit_delta,
    compute_delta,
    derive_session,
    normalize_messages,
    response_messages,
    seed_state,
    should_skip,
    transcript,
)
from sinks import Sink, sink_from_env  # noqa: E402

log = logging.getLogger("memoryhub_capture")


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
            "memoryhub_capture: ready (sink=%s extract_every=%d tools=%s max_bytes=%d)",
            os.environ.get("MEMORYHUB_CAPTURE_SINK", "jsonl"), self.extract_every,
            self.capture_tools, self.max_message_bytes,
        )
        self.observations_path.parent.mkdir(parents=True, exist_ok=True)
        self.sessions: dict[str, SessionState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._obs_lock = asyncio.Lock()

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
        session = derive_session(slo, full, kwargs)

        skip = should_skip(slo, full, ignore_models=self.ignore_models)
        if skip:
            await self._observe(build_observation(slo, session, full, None, skipped=skip))
            return

        lock = self._locks.setdefault(session.key, asyncio.Lock())
        async with lock:
            state = self.sessions.setdefault(session.key, SessionState())
            obs = build_observation(slo, session, full, compute_delta(state, full))
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

                meta = {
                    "source": "litellm-proxy",
                    "call_id": obs.call_id,
                    "model": obs.model,
                    "session_source": session.source,
                }
                if delta.history_rewritten:
                    meta["after_history_rewrite"] = True
                for i, msg in enumerate(delta.new, start=delta.prefix_len + 1):
                    if not (msg.role in ("tool_call", "tool_result") and not self.capture_tools):
                        await self.sink.append(state.thread_id, msg, actor_id=session.actor_id, metadata=meta)
                        obs.appended += 1
                        if msg.role == "user":
                            state.user_turns += 1
                    # commit per message: a failure mid-way must not cause the
                    # already-stored messages to be appended again next time
                    commit_delta(state, full[:i])

                if self.extract_every and state.user_turns >= self.extract_every:
                    state.user_turns = 0
                    t0 = time.monotonic()
                    obs.extraction = await self.sink.extract(state.thread_id)
                    obs.extract_ms = (time.monotonic() - t0) * 1000
                    if obs.extraction and obs.extraction.get("failures"):
                        # MemoryHub advances the cursor past failed windows, so these
                        # messages will not be retried automatically.
                        log.warning(
                            "memoryhub_capture: extraction had %s failed window(s) on thread %s; "
                            "check the MCP pod logs and re-run with `verify.py reextract`",
                            obs.extraction["failures"], state.thread_id,
                        )
            except Exception as exc:
                obs.error = f"{type(exc).__name__}: {exc}"
                log.warning("memoryhub_capture: sink error for %s: %s", session.key, exc)
            await self._observe(obs)

    async def _observe(self, obs: Observation) -> None:
        async with self._obs_lock:
            with self.observations_path.open("a", encoding="utf-8") as fh:
                fh.write(obs.to_json() + "\n")
        log.info(
            "memoryhub_capture: session=%s(%s) new=%d appended=%d skipped=%s rewritten=%s",
            obs.session_key[:12], obs.session_source, obs.new_messages, obs.appended,
            obs.skipped, obs.history_rewritten,
        )


proxy_handler_instance = MemoryHubCaptureLogger()
