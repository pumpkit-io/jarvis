"""The Barge-in decision: from what the User said over Jarvis so far, whether Jarvis resumes or stops.

Pure, like the voice state machine that calls it. The lexicons are data files beside this module, read once.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Decision = Literal["wait", "resume", "stop"]

# A gap in VAD this short is between words; longer, the User has stopped.
TALKING_GAP_S = 0.2

LEXICON_DIR = Path(__file__).parent / "lexicons"

_WORDS = re.compile(r"[\w']+")
# How STT marks sounds that are not words: "[BLANK_AUDIO]", "[Music]", "(coughs)".
_NON_SPEECH = re.compile(r"\[[^\]]*\]|\([^)]*\)")


def words(text: str) -> tuple[str, ...]:
    """Lowercase words, with hyphens, punctuation and STT's non-speech marks gone and curly apostrophes made straight."""
    return tuple(_WORDS.findall(_NON_SPEECH.sub(" ", text).lower().replace("’", "'")))


@dataclass(frozen=True)
class Partial:
    """What STT made of the User's speech since the Barge-in's onset, and how that speech stood at `at`."""

    text: str
    at: float
    # From the onset to the latest speech VAD heard.
    speech_s: float
    # Since the latest speech VAD heard.
    silent_s: float


@dataclass(frozen=True)
class Lexicons:
    """Phrases as space-joined `words`."""

    backchannel: frozenset[str]
    interrupt: frozenset[str]

    @staticmethod
    def load(folder: Path) -> "Lexicons":
        return Lexicons(_phrases(folder / "backchannel.txt"), _phrases(folder / "interrupt.txt"))

    def interrupts(self, said: Sequence[str]) -> bool:
        """Whether a hard interrupt was said, other than inside a longer Backchannel phrase ("no" in "no problem")."""
        backchannels = [(start, start + len(phrase)) for phrase, start in _occurrences(said, self.backchannel)]
        return any(
            not any(s <= start and start + len(phrase) <= e and e - s > len(phrase) for s, e in backchannels)
            for phrase, start in _occurrences(said, self.interrupt)
        )


def _occurrences(said: Sequence[str], phrases: frozenset[str]) -> Iterable[tuple[tuple[str, ...], int]]:
    """Each phrase found in `said`, as its words and where it starts."""
    for phrase in phrases:
        split = tuple(phrase.split())
        for start in range(len(said) - len(split) + 1):
            if tuple(said[start : start + len(split)]) == split:
                yield split, start


def _phrases(path: Path) -> frozenset[str]:
    lines: Iterable[str] = path.read_text(encoding="utf-8").splitlines()
    return frozenset(" ".join(w) for line in lines if not line.lstrip().startswith("#") and (w := words(line)))


LEXICONS = Lexicons.load(LEXICON_DIR)


def interrupts(text: str, lexicons: Lexicons = LEXICONS) -> bool:
    """Whether `text` holds a hard interrupt, the Wake word among them."""
    return lexicons.interrupts(words(text))


def still_talking(partial: Partial) -> bool:
    return partial.silent_s < TALKING_GAP_S


def decide(partials: Sequence[Partial], *, final: bool, lexicons: Lexicons = LEXICONS) -> Decision:
    """Stop, resume, or wait for the next partial; with `final`, at the deadline, never wait.

    Only the latest partial counts: each covers everything since the onset.
    """
    if not partials:
        return "resume" if final else "wait"
    latest = partials[-1]
    if lexicons.interrupts(words(latest.text)):
        return "stop"
    # Other words are no strong evidence (ADR 0014): STT makes them of echo residue and distant speech too.
    return "resume" if not still_talking(latest) or final else "wait"
