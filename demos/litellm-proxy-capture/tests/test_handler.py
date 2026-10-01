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


@pytest.fixture
def make_logger(tmp_path, monkeypatch):
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
    asyncio.run(lg.async_log_success_event(_kwargs([{"role": "user", "content": "x"}], "y"), None, 0, 0))
    assert sink.extracted, "extraction should trigger after the env var was set post-import"


def test_module_level_instance_picks_up_extract_every(monkeypatch, tmp_path):
    """The instance LiteLLM actually uses is created at import time."""
    import memoryhub_capture

    lg = memoryhub_capture.proxy_handler_instance
    sink = FakeSink()
    previous_sink, previous_obs = lg.sink, lg.observations_path
    lg.sink = sink
    lg.observations_path = tmp_path / "obs.jsonl"
    lg.sessions.clear()
    monkeypatch.setenv("MEMORYHUB_CAPTURE_EXTRACT_EVERY", "1")
    monkeypatch.setenv("MEMORYHUB_CAPTURE_ENABLED", "true")
    try:
        asyncio.run(lg.async_log_success_event(
            _kwargs([{"role": "user", "content": "x"}], "y", session="mod-inst"), None, 0, 0))
        assert sink.extracted, "module-level instance must honour EXTRACT_EVERY set after import"
    finally:
        lg.sink = previous_sink
        lg.observations_path = previous_obs
        lg.sessions.clear()
        monkeypatch.delenv("MEMORYHUB_CAPTURE_EXTRACT_EVERY", raising=False)
