"""The wrong-resume heuristic of the Barge-in log. The log's rows are checked through voice mode in test_voice_mode.py."""

import pytest

from jarvis_hub.voice.bargein_log import repeated_themselves


@pytest.mark.parametrize(
    ("cut_off", "next_turn", "repeated"),
    [
        (True, "Which is the second one?", True),
        (True, "", True),
        (False, "Wait, which is the second one?", True),
        (False, "No, the other repo", True),
        (False, "Actually open an issue instead", True),
        (False, "Aspetta, un attimo", True),
        (False, "Thanks, and what is the weather?", False),
        (False, "No problem, carry on", False),
        (False, "", False),
    ],
)
def test_a_resume_was_likely_wrong_when_the_next_turn_cut_jarvis_off_or_holds_an_interrupt(
    cut_off: bool, next_turn: str, repeated: bool
) -> None:
    assert repeated_themselves(cut_off, next_turn) is repeated
