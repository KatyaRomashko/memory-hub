"""Tests for the offline test doubles and the duplicate detector."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from fake_extractor import facts_from_window  # noqa: E402
from verify import repeated_block  # noqa: E402


def test_extractor_keeps_facts_and_drops_chatter():
    window = (
        "[USER] (seq=1): Hi! Quick context about me: I prefer dark mode everywhere.\n"
        "[ASSISTANT] (seq=2): Got it, noted.\n"
        "[USER] (seq=3): Thanks!"
    )
    facts = facts_from_window(window)
    assert [f["content"] for f in facts] == ["I prefer dark mode everywhere"]


def test_extractor_ignores_assistant_turns():
    assert facts_from_window("[ASSISTANT] (seq=2): I prefer dark mode everywhere.") == []


def test_repeated_block_ignores_normal_repeats():
    rows = [("user", "u1"), ("assistant", "ok"), ("user", "u2"), ("assistant", "ok")]
    assert repeated_block(rows) is None
    assert repeated_block(rows + rows) == (0, 4, 4)
