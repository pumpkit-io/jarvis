from dataclasses import dataclass

import pytest

from jarvis_hub.voice.engines import words_after


@dataclass
class Token:
    text: str
    start: float
    end: float


# "A Jarvus, cancel that." as parakeet times it: the wake phrase misheard, then the request.
TOKENS = [
    Token(" A", 0.0, 0.16),
    Token(" J", 0.24, 0.32),
    Token("ar", 0.32, 0.48),
    Token("vus", 0.48, 0.64),
    Token(",", 0.64, 0.72),
    Token(" can", 0.88, 1.04),
    Token("cel", 1.04, 1.2),
    Token(" that", 1.28, 1.52),
    Token(".", 1.52, 1.6),
]


@pytest.mark.parametrize(
    ("after", "text"),
    [
        (0.0, "A Jarvus, cancel that."),
        (0.7, "cancel that."),
        (0.66, "cancel that."),
        (1.1, "cancel that."),
        (1.2, "that."),
        (2.0, ""),
    ],
)
def test_words_after_the_wake_word_hit(after: float, text: str) -> None:
    assert words_after(TOKENS, after) == text
