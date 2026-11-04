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
# Speech still going this long is a bid for the floor, whatever STT has made of it so far.
LONG_SPEECH_S = 0.7
# This many words that are not all Backchannel take the floor ("okay but what about…").
FLOOR_WORDS = 3

LEXICON_DIR = Path(__file__).parent / "lexicons"

_WORDS = re.compile(r"[\w']+")


def words(text: str) -> tuple[str, ...]:
    """Lowercase words, with hyphens and punctuation gone and curly apostrophes made straight."""
    return tuple(_WORDS.findall(text.lower().replace("’", "'")))


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
        padded = f" {' '.join(said)} "
        return any(f" {phrase} " in padded for phrase in self.interrupt)

    def only_backchannel(self, said: Sequence[str]) -> bool:
        """Whether `said` is Backchannel phrases end to end."""
        longest = max((len(p.split()) for p in self.backchannel), default=0)
        # covered[i]: the first i words split into Backchannel phrases.
        covered = [True] + [False] * len(said)
        for end in range(1, len(said) + 1):
            covered[end] = any(
                covered[end - n] and " ".join(said[end - n : end]) in self.backchannel
                for n in range(1, min(longest, end) + 1)
            )
        return covered[-1]


def _phrases(path: Path) -> frozenset[str]:
    lines: Iterable[str] = path.read_text(encoding="utf-8").splitlines()
    return frozenset(" ".join(w) for line in lines if not line.lstrip().startswith("#") and (w := words(line)))


LEXICONS = Lexicons.load(LEXICON_DIR)


def decide(partials: Sequence[Partial], *, wake: bool, final: bool, lexicons: Lexicons = LEXICONS) -> Decision:
    """Stop, resume, or wait for the next partial; with `final`, at the deadline, never wait.

    Only the latest partial counts: each covers everything since the onset.
    """
    if wake:
        return "stop"
    if not partials:
        return "resume" if final else "wait"
    latest = partials[-1]
    said = words(latest.text)
    if lexicons.interrupts(said):
        return "stop"
    talking = latest.silent_s < TALKING_GAP_S
    if said and not lexicons.only_backchannel(said):
        floor = len(said) >= FLOOR_WORDS or latest.speech_s >= LONG_SPEECH_S
        return "stop" if floor or not talking or final else "wait"
    if not talking:
        return "resume"
    if final:
        # A Backchannel still going at the deadline may be the start of a sentence; noise is not.
        return "stop" if said else "resume"
    return "wait"
