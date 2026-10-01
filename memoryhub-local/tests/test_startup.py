"""Startup should not dump HuggingFace tracebacks in quiet/offline mode."""

import asyncio


def test_offline_skips_download_and_uses_mock(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    called: list = []
    monkeypatch.setattr("memoryhub_local.startup.download_model", lambda d: called.append(d))
    monkeypatch.setattr("memoryhub_local.startup.is_model_downloaded", lambda d: False)
    from memoryhub_local.startup import initialize_backend

    state = asyncio.run(initialize_backend(quiet=True))
    assert called == []
    assert type(state.embedding_service).__name__.startswith("Mock")
