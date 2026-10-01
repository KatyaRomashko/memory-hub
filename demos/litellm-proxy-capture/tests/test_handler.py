import asyncio

import pytest

pytest.importorskip("litellm")


class FakeSink:
    def __init__(self, fail_append=False, fail_on_call=None, existing=None):
        self.threads, self.appended, self.extracted = [], [], []
        self.fail_append = fail_append
        self.fail_on_call = fail_on_call
        self.existing = existing
        self.calls = 0

    async def ensure_thread(self, session):
        from sinks import ThreadHandle
        self.threads.append(session.key)
        if self.existing is not None:
            return ThreadHandle("t-old", existing=self.existing, reused=True)
        return ThreadHandle(f"t-{len(self.threads)}")

    async def append(self, thread_id, msg, *, actor_id, metadata):
        self.calls += 1
        if self.fail_append or self.calls == self.fail_on_call:
            raise RuntimeError("memoryhub down")
        self.appended.append((thread_id, msg.role, msg.content))

    async def extract(self, thread_id):
        self.extracted.append(thread_id)
        return {"extracted_count": 1, "failures": 0}


def _kwargs(history, reply, session="s1", call_type="acompletion"):
    return {"standard_logging_object": {
        "status": "success", "call_type": call_type, "model_group": "poc-model",
        "messages": history,
        "response": {"choices": [{"message": {"role": "assistant", "content": reply}}]},
        "metadata": {"requester_custom_headers": {"x-memoryhub-session": session}},
        "model_parameters": {},
    }}


def fire(lg, *kwargs_list):
    """Deliver call(s) to the callback and wait for the detached extraction pass."""
    async def run():
        for kw in kwargs_list:
            await lg.async_log_success_event(kw, None, 0, 0)
        await lg.drain()
    asyncio.run(run())


@pytest.fixture
def make_logger(tmp_path, monkeypatch):
    # The operator's shell usually has the demo environment sourced
    # (live/env.sh exports MEMORYHUB_CAPTURE_EXTRACT_EVERY and friends), which
    # would leak into every logger these tests build. Start from a clean slate.
    import os
    for name in [k for k in os.environ if k.startswith("MEMORYHUB_CAPTURE_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMORYHUB_CAPTURE_OBSERVATIONS", str(tmp_path / "obs.jsonl"))
    monkeypatch.setenv("MEMORYHUB_CAPTURE_THREADS_LOG", str(tmp_path / "threads.jsonl"))
    import memoryhub_capture

    def _make(sink, **env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return memoryhub_capture.MemoryHubCaptureLogger(sink=sink)
    return _make


def test_two_turns_one_thread_and_extraction(make_logger):
    sink = FakeSink()
    lg = make_logger(sink, MEMORYHUB_CAPTURE_EXTRACT_EVERY="2")
    h1 = [{"role": "user", "content": "I prefer dark mode"}]
    h2 = h1 + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "and Python"}]

    async def run():
        await lg.async_log_success_event(_kwargs(h1, "ok"), None, 0, 0)
        await lg.async_log_success_event(_kwargs(h2, "noted"), None, 0, 0)
        await lg.drain()  # extraction runs detached from the proxy's callback
    asyncio.run(run())

    assert sink.threads == ["s1"]
    assert [c for _, _, c in sink.appended] == ["I prefer dark mode", "ok", "and Python", "noted"]
    assert sink.extracted == ["t-1"]


def test_sink_failure_does_not_raise_and_is_observed(make_logger, tmp_path):
    lg = make_logger(FakeSink(fail_append=True))
    asyncio.run(lg.async_log_success_event(_kwargs([{"role": "user", "content": "x"}], "y"), None, 0, 0))
    assert "memoryhub down" in (tmp_path / "obs.jsonl").read_text()


def test_skipped_calls_are_observed_not_captured(make_logger, tmp_path):
    sink = FakeSink()
    lg = make_logger(sink)
    asyncio.run(lg.async_log_success_event(
        _kwargs([{"role": "user", "content": "x"}], "y", call_type="aembedding"), None, 0, 0))
    assert sink.appended == [] and '"skipped": "call_type:aembedding"' in (tmp_path / "obs.jsonl").read_text()


def test_partial_failure_does_not_duplicate(make_logger):
    sink = FakeSink(fail_on_call=2)  # user stored, assistant append fails
    lg = make_logger(sink)
    h1 = [{"role": "user", "content": "u1"}]
    h2 = h1 + [{"role": "assistant", "content": "a1"}, {"role": "user", "content": "u2"}]

    async def run():
        await lg.async_log_success_event(_kwargs(h1, "a1"), None, 0, 0)
        await lg.async_log_success_event(_kwargs(h2, "a2"), None, 0, 0)
    asyncio.run(run())
    assert [c for _, _, c in sink.appended] == ["u1", "a1", "u2", "a2"]


def test_reused_thread_is_seeded(make_logger):
    from capture_core import Message
    sink = FakeSink(existing=[Message("user", "u1"), Message("assistant", "a1")])
    lg = make_logger(sink)
    h = [{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"},
         {"role": "user", "content": "u2"}]
    asyncio.run(lg.async_log_success_event(_kwargs(h, "a2"), None, 0, 0))
    assert sink.appended == [("t-old", "user", "u2"), ("t-old", "assistant", "a2")]


def test_session_error_detection():
    from sinks import _is_session_error
    assert _is_session_error(RuntimeError("McpError: Session terminated"))
    assert not _is_session_error(ValueError("Not authorized to append to this thread."))


def test_flags_are_re_read_from_the_environment(make_logger, monkeypatch):
    """LiteLLM imports the callback at proxy start, which can happen before the
    operator's environment is complete. The flags must not freeze at import."""
    sink = FakeSink()
    lg = make_logger(sink)                      # created with EXTRACT_EVERY unset
    assert lg.extract_every == 0
    monkeypatch.setenv("MEMORYHUB_CAPTURE_EXTRACT_EVERY", "1")
    fire(lg, _kwargs([{"role": "user", "content": "x"}], "y"))
    assert sink.extracted, "extraction should trigger after the env var was set post-import"


AGENT_WRITE_TURN = [
    {"role": "user", "content": "Remember that I prefer dark mode"},
    {"role": "assistant", "content": None,
     "tool_calls": [{"function": {"name": "write_memory", "arguments": '{"content":"prefers dark mode"}'}}]},
    {"role": "tool", "content": '{"memory": {"id": "7d2f01c5-1ad1-5873-8fca-b4914152b302"}}'},
]


def test_agent_write_defers_extraction(make_logger):
    """The agent saved this turn itself: keep the transcript, skip the extra
    extraction pass, leave the messages for a later dreaming run."""
    sink = FakeSink()
    lg = make_logger(sink, MEMORYHUB_CAPTURE_EXTRACT_EVERY="1",
                     MEMORYHUB_CAPTURE_WHEN_AGENT_WRITES="defer",
                     MEMORYHUB_CAPTURE_TOOLS="true")
    fire(lg, _kwargs(AGENT_WRITE_TURN, "Saved."))
    assert sink.appended, "the thread must still receive the turn"
    assert sink.extracted == [], "extraction should be deferred"


def test_agent_write_can_skip_the_thread_entirely(make_logger):
    sink = FakeSink()
    lg = make_logger(sink, MEMORYHUB_CAPTURE_EXTRACT_EVERY="1",
                     MEMORYHUB_CAPTURE_WHEN_AGENT_WRITES="skip-thread")
    fire(lg, _kwargs(AGENT_WRITE_TURN, "Saved."))
    assert sink.appended == [] and sink.extracted == []


def test_turn_without_agent_write_still_extracts(make_logger):
    sink = FakeSink()
    lg = make_logger(sink, MEMORYHUB_CAPTURE_EXTRACT_EVERY="1",
                     MEMORYHUB_CAPTURE_WHEN_AGENT_WRITES="defer")
    fire(lg, _kwargs([{"role": "user", "content": "I always use Python"}], "Noted."))
    assert sink.extracted, "a turn the agent did not cover must still be extracted"


def test_memory_search_is_not_a_write(make_logger):
    """Reading memory does not mean the agent stored anything."""
    sink = FakeSink()
    lg = make_logger(sink, MEMORYHUB_CAPTURE_EXTRACT_EVERY="1",
                     MEMORYHUB_CAPTURE_WHEN_AGENT_WRITES="defer",
                     MEMORYHUB_CAPTURE_TOOLS="true")
    turn = [
        {"role": "user", "content": "What do you know about my setup?"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"function": {"name": "memory", "arguments": '{"action":"search","query":"setup"}'}}]},
        {"role": "tool", "content": "[]"},
    ]
    fire(lg, _kwargs(turn, "Nothing yet."))
    assert sink.extracted, "a search must not count as the agent saving something"


def test_extraction_does_not_block_the_callback(make_logger):
    """LiteLLM cancels any callback that runs longer than its logging-worker
    timeout (20s by default). Extraction is a second LLM call, so it must not
    happen inside the callback: MemoryHub commits each memory as it is created
    but commits the extraction cursor only at the end, and a cancellation in
    between leaves memories stored with the cursor unmoved -- the same messages
    are extracted again on the next pass."""
    class SlowSink(FakeSink):
        async def extract(self, thread_id):
            await asyncio.sleep(0.2)
            return await super().extract(thread_id)

    sink = SlowSink()
    lg = make_logger(sink, MEMORYHUB_CAPTURE_EXTRACT_EVERY="1")

    async def run():
        import time as _t
        t0 = _t.monotonic()
        await lg.async_log_success_event(
            _kwargs([{"role": "user", "content": "I use Python"}], "Noted."), None, 0, 0)
        callback_ms = (_t.monotonic() - t0) * 1000
        assert sink.extracted == [], "extraction must not be awaited inside the callback"
        assert callback_ms < 150, f"callback waited for extraction ({callback_ms:.0f} ms)"
        await lg.drain()
        assert sink.extracted == ["t-1"], "the detached pass must still run"
    asyncio.run(run())


def test_a_running_extraction_does_not_block_the_next_call(make_logger):
    """The bug that silently lost a turn in demo run 2.

    An extraction pass takes tens of seconds. While it ran, the next call's
    callback waited on the same per-session lock, and LiteLLM's logging worker
    cancelled it at 20s -- so that turn was never stored, while the user got a
    perfectly good answer and nothing in the terminal looked wrong. Capture and
    extraction now take separate locks.
    """
    class SlowSink(FakeSink):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()

        async def extract(self, thread_id):
            self.started.set()
            await asyncio.sleep(1.0)
            return await super().extract(thread_id)

    sink = SlowSink()
    lg = make_logger(sink, MEMORYHUB_CAPTURE_EXTRACT_EVERY="1")

    async def run():
        import time as _t
        await lg.async_log_success_event(
            _kwargs([{"role": "user", "content": "first turn"}], "ok"), None, 0, 0)
        await asyncio.wait_for(sink.started.wait(), timeout=2)   # extraction in flight

        t0 = _t.monotonic()
        await lg.async_log_success_event(
            _kwargs([{"role": "user", "content": "first turn"},
                     {"role": "assistant", "content": "ok"},
                     {"role": "user", "content": "second turn"}], "ok too"), None, 0, 0)
        waited_ms = (_t.monotonic() - t0) * 1000
        assert waited_ms < 300, f"callback waited {waited_ms:.0f} ms on a running extraction"
        assert any(role == "user" and text == "second turn"
                   for _, role, text in sink.appended), "the second turn was dropped"
        await lg.drain()
    asyncio.run(run())
