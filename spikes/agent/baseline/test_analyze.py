"""Synthetic checks for analyze.compute_turns: a missing hook entry must not shift the split.

Run: python3 -m pytest spikes/agent/baseline/test_analyze.py   (or python3 test_analyze.py)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze import compute_turns  # noqa: E402

SILENCE = 1.0


def call(start, rec_end, stt, end, first_audio=None):
    c = {"start": start, "RECORDING_END": rec_end, "STT_COMPLETE": stt, "TOOL_REQUEST_END": end}
    if first_audio is not None:
        c["TTS_FIRST_AUDIO"] = first_audio
    return c


# Call A speaks and listens; B and C each speak. Hook entries sit just outside each call's own span.
A = call(0, 10, 11, 11.1)
B = call(14, 24, 25, 25.1, first_audio=14.8)
C = call(30, 40, 41, 41.1, first_audio=30.8)


def test_complete_hooks_give_the_real_split():
    turns, skipped = compute_turns([A, B], pre=[-0.1, 13.8], post=[11.2], other_pre=[], silence=SILENCE)
    t = turns[0]
    assert skipped == 0
    assert round(t["tool_return"], 2) == 0.2   # PostToolUse at 11.2, STT done at 11.0
    assert round(t["model"], 2) == 2.6         # next PreToolUse at 13.8
    assert round(t["tts_first_audio"], 2) == 1.0


def test_missing_post_tool_use_does_not_borrow_the_next_calls_hook():
    # A's PostToolUse is missing; B's is at 25.2 and B's PreToolUse for C is at 29.8.
    turns, skipped = compute_turns([A, B, C], pre=[-0.1, 13.8, 29.8], post=[25.2, 41.2], other_pre=[], silence=SILENCE)
    first = turns[0]
    assert skipped == 0
    assert round(first["tool_return"], 2) == 0.1  # falls back to VoiceMode's own tool end (11.1)
    assert round(first["model"], 2) == 2.7        # 13.8 - 11.1, not 29.8 - 25.2
    assert first["model"] > 0 and first["tts_first_audio"] > 0


def test_inconsistent_turn_is_skipped_and_counted():
    # B's first audio precedes its own PreToolUse: the hook log disagrees with VoiceMode.
    bad = dict(B, TTS_FIRST_AUDIO=13.0)
    turns, skipped = compute_turns([A, bad], pre=[-0.1, 13.8], post=[11.2], other_pre=[], silence=SILENCE)
    assert turns == [] and skipped == 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
