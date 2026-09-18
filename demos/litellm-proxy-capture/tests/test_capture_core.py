import re

from capture_core import (
    Message,
    SessionState,
    commit_delta,
    compute_delta,
    derive_session,
    normalize_messages,
    response_messages,
    should_skip,
    transcript,
)


def _openai_resp(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def test_normalize_openai_and_tool_calls():
    msgs = normalize_messages([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {}}]},
        {"role": "assistant", "content": None,
         "tool_calls": [{"function": {"name": "ls", "arguments": "{}"}}]},
        {"role": "tool", "content": "a.txt"},
    ])
    assert [m.role for m in msgs] == ["system", "user", "tool_call", "tool_result"]
    assert "[image_url omitted]" in msgs[1].content


def test_normalize_anthropic_blocks_and_thinking_dropped():
    msgs = normalize_messages({
        "system": [{"type": "text", "text": "S"}],
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "secret"},
                {"type": "text", "text": "let me look"},
                {"type": "tool_use", "name": "Read", "input": {"p": 1}},
            ]},
            {"role": "user", "content": [{"type": "tool_result", "content": [{"type": "text", "text": "ok"}]}]},
        ],
    })
    assert [m.role for m in msgs] == ["system", "user", "assistant", "tool_call", "tool_result"]
    assert all("secret" not in m.content for m in msgs)


def test_response_shapes():
    assert response_messages(_openai_resp("yo")) == [Message("assistant", "yo")]
    anth = {"type": "message", "content": [{"type": "text", "text": "yo"}]}
    assert response_messages(anth) == [Message("assistant", "yo")]
    assert response_messages(None) == []


def _full(history, reply):
    return transcript(normalize_messages(history) + response_messages(_openai_resp(reply)))


def test_delta_only_new_messages_across_turns():
    state = SessionState()
    h = [{"role": "system", "content": "sys @ 10:00"}, {"role": "user", "content": "u1"}]
    full = _full(h, "a1")
    d = compute_delta(state, full)
    assert [m.content for m in d.new] == ["u1", "a1"] and not d.history_rewritten
    commit_delta(state, full)

    # next call resends history; system prompt changed (volatile) -> ignored
    h2 = [{"role": "system", "content": "sys @ 10:05"}, {"role": "user", "content": "u1"},
          {"role": "assistant", "content": "a1"}, {"role": "user", "content": "u2"}]
    full2 = _full(h2, "a2")
    d2 = compute_delta(state, full2)
    assert [m.content for m in d2.new] == ["u2", "a2"] and not d2.history_rewritten
    commit_delta(state, full2)

    # retry of the same call -> nothing new
    assert compute_delta(state, full2).new == []


def test_delta_detects_history_rewrite():
    state = SessionState()
    full = _full([{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"},
                  {"role": "user", "content": "u2"}], "a2")
    commit_delta(state, full)
    compacted = _full([{"role": "user", "content": "u1"}, {"role": "user", "content": "summary of a1/u2/a2"},
                       {"role": "user", "content": "u3"}], "a3")
    d = compute_delta(state, compacted)
    assert d.history_rewritten and d.prefix_len == 1
    assert [m.content for m in d.new] == ["summary of a1/u2/a2", "u3", "a3"]


def test_session_priority():
    msgs = [Message("user", "hello")]
    header = derive_session({"metadata": {"requester_custom_headers": {"X-MemoryHub-Session": "s1",
                                                                       "x-memoryhub-actor": "kate"}}}, msgs)
    assert (header.key, header.source, header.actor_id, header.actor_source) == ("s1", "header", "kate", "header")

    lsess = derive_session({"session_id": "abc", "trace_id": "t"}, msgs)
    assert (lsess.key, lsess.source) == ("abc", "litellm_session")

    # LiteLLM defaults session_id to trace_id when the client sends none
    fp = derive_session({"session_id": "t", "trace_id": "t"}, msgs)
    assert fp.source == "fingerprint"
    assert derive_session({}, msgs).key == fp.key

    user = derive_session({"end_user": "u-7"}, msgs)
    assert user.source == "end_user_fingerprint" and user.key.startswith("u-7:")


def test_headers_from_proxy_request_and_auth_stripped():
    kwargs = {"litellm_params": {"proxy_server_request": {"headers": {
        "Authorization": "Bearer x", "x-memoryhub-session": "s2"}}}}
    s = derive_session({}, [Message("user", "x")], kwargs)
    assert s.key == "s2"


def test_should_skip():
    msgs = [Message("user", "x")]
    assert should_skip({"status": "success", "call_type": "acompletion"}, msgs, ignore_models=None) is None
    assert should_skip({"call_type": "aembedding"}, msgs, ignore_models=None).startswith("call_type")
    assert should_skip({"call_type": "anthropic_messages"}, msgs, ignore_models=None) is None
    assert should_skip({"model": "claude-haiku-4"}, msgs, ignore_models=re.compile("haiku")).startswith("ignored")
    assert should_skip({}, [Message("assistant", "x")], ignore_models=None) == "no_user_message"


def test_system_reminders_stripped_and_long_text_truncated():
    from capture_core import TRUNCATION_MARKER, truncate_bytes
    msgs = normalize_messages([{"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>\nhook output\n</system-reminder>"},
        {"type": "text", "text": "real question"},
    ]}])
    assert msgs == [Message("user", "real question")]
    only_reminder = normalize_messages([{"role": "user", "content": "<system-reminder>x</system-reminder>"}])
    assert only_reminder == []

    long = "я" * 5000  # 10000 bytes
    cut = truncate_bytes(long, 8000)
    assert len(cut.encode()) <= 8000 and cut.endswith(TRUNCATION_MARKER)
    assert transcript([Message("user", long)])[0].content == cut


def test_seed_state_prevents_reappending_after_restart():
    from capture_core import seed_state
    stored = [Message("user", "u1"), Message("assistant", "a1")]
    state = SessionState()
    seed_state(state, stored)
    full = stored + [Message("user", "u2"), Message("assistant", "a2")]
    assert [m.content for m in compute_delta(state, full).new] == ["u2", "a2"]
