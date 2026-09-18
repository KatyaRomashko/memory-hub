"""Open the personal-edition backend without the noisy startup output.

``initialize_backend`` logs the HuggingFace download failure with a full
traceback and prints a warning to stderr. That is right for a real install and
wrong for a demo, so this helper silences it and prints one line instead:

    embeddings: onnx | mock (model not downloaded -- search ranking is meaningless)
"""

from __future__ import annotations

import logging
import sys


async def open_backend(*, announce: bool = True):
    startup_log = logging.getLogger("memoryhub_local.startup")
    previous = startup_log.level
    startup_log.setLevel(logging.CRITICAL)
    try:
        from memoryhub_local.startup import initialize_backend

        state = await initialize_backend(quiet=True)
    finally:
        startup_log.setLevel(previous)

    kind = "mock" if type(state.embedding_service).__name__.startswith("Mock") else "onnx"
    if announce:
        note = " (model not downloaded -- search ranking is meaningless)" if kind == "mock" else ""
        print(f"embeddings: {kind}{note}", file=sys.stderr)
    return state, kind
