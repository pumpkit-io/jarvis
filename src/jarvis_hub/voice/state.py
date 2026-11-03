"""The voice state machine: when Jarvis listens, replies and goes idle.

Pure: `step` takes the current `Voice`, one event stamped with its time, and the timing settings, and returns the
next `Voice` with the actions to carry out. Time passes only through events, `Tick` among them.
"""

import re
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Literal

from jarvis_hub.voice.language import Lang

VoiceState = Literal["idle", "listening", "thinking", "speaking", "follow_up", "announce"]

# The states in which the User's speech becomes a turn.
CAPTURING: frozenset[VoiceState] = frozenset({"listening", "follow_up"})


@dataclass(frozen=True)
class Timing:
    follow_up_s: float
    # After an Announcement, a shorter window than after a reply.
    announce_follow_up_s: float
    # After the Wake word, how long the User has to start speaking.
    listen_s: float
    # Wake word hits this soon after the chime starts are the chime's own echo or the same phrase, and are ignored.
    chime_s: float


@dataclass(frozen=True)
class WakeWord:
    at: float


@dataclass(frozen=True)
class SpeechStarted:
    at: float


@dataclass(frozen=True)
class Heard:
    """The User's turn, transcribed once its end was detected. `lang` is None when the STT does not report it."""

    text: str
    lang: Lang | None
    at: float


@dataclass(frozen=True)
class ReplyAudio:
    """A reply's first audio started playing."""

    at: float


@dataclass(frozen=True)
class ReplyDone:
    """A reply has finished playing, or ended without any audio."""

    at: float


@dataclass(frozen=True)
class EndConversation:
    """The Talker called `end_conversation`: idle once the reply has played, with no Follow-up window."""

    at: float


@dataclass(frozen=True)
class Tick:
    at: float


Event = WakeWord | SpeechStarted | Heard | ReplyAudio | ReplyDone | EndConversation | Tick


@dataclass(frozen=True)
class Chime:
    pass


@dataclass(frozen=True)
class Say:
    """A cached line."""

    name: Literal["very_good"]
    lang: Lang


@dataclass(frozen=True)
class SendTurn:
    text: str
    lang: Lang | None
    at: float


@dataclass(frozen=True)
class Interrupt:
    """Stop the reply in progress, both the Talker's turn and its audio."""


Action = Chime | Say | SendTurn | Interrupt


@dataclass(frozen=True)
class Voice:
    state: VoiceState = "idle"
    # When a Tick sends the state on (from listening or the Follow-up window to idle); None while nothing is due.
    deadline: float | None = None
    chime_until: float = float("-inf")
    ending: bool = False


# Said on their own, after punctuation and the wake phrase are gone. Anything longer goes to the Talker.
STOP_PHRASES: dict[str, Lang] = {
    "that's all": "en",
    "that's all thanks": "en",
    "that's all thank you": "en",
    "thanks jarvis": "en",
    "thank you jarvis": "en",
    "thanks jarvis that's all": "en",
    "stand by": "en",
    "go to sleep": "en",
    "grazie jarvis": "it",
    "basta così": "it",
    "basta cosi": "it",
    "grazie jarvis basta così": "it",
    "è tutto": "it",
}

# Hesitations STT makes of a breath or a cough; a turn of nothing else is noise.
_NOISE_WORDS = frozenset("uh um uhm hm hmm mm mmm ah eh ehm er erm".split())

# The wake phrase, with whatever came before it in the pre-roll: a greeting then "Jarvis" anywhere, or "Jarvis" first.
_WAKE_PHRASE = re.compile(r"^(?:.*?\b(?:hey|hi|ehi|ehy|ok|okay)\W+|\W*)jarvis\b(?!')\W*", re.IGNORECASE)
_WORDS = re.compile(r"[\w']+")


def _words(text: str) -> list[str]:
    return [w.lower().replace("’", "'") for w in _WORDS.findall(text.replace("’", "'"))]


# What is left of "Jarvis" when the Wake word fired before STT's end of the word: "Harvis", "Jarvid", "Jorvis".
_FIRST_WORD = re.compile(r"^(\w+)(?!')\W+(?=\w)")


def _sounds_like_jarvis(word: str) -> bool:
    return SequenceMatcher(None, word.lower(), "jarvis").ratio() >= 0.8


def without_wake_phrase(text: str) -> str:
    text = _WAKE_PHRASE.sub("", text.strip(), count=1)
    if (first := _FIRST_WORD.match(text)) and _sounds_like_jarvis(first[1]):
        text = text[first.end() :]
    return text


def _is_noise(text: str) -> bool:
    return all(w in _NOISE_WORDS for w in _words(text))


def stop_phrase(text: str) -> Lang | None:
    """The language of `text` when it is a stop phrase and nothing else."""
    return STOP_PHRASES.get(" ".join(_words(text)))


def step(voice: Voice, event: Event, timing: Timing) -> tuple[Voice, list[Action]]:
    match event:
        case WakeWord(at):
            return _wake(voice, at, timing)
        case SpeechStarted():
            if voice.state in CAPTURING:
                return replace(voice, state="listening", deadline=None), []
            return voice, []
        case Heard(text, lang, at):
            return _heard(voice, text, lang, at, timing)
        case ReplyAudio():
            if voice.state == "idle":
                return replace(voice, state="announce", deadline=None), []
            if voice.state in ("thinking", "follow_up"):
                return replace(voice, state="speaking", deadline=None), []
            return voice, []
        case ReplyDone(at):
            if voice.state == "announce":
                return replace(voice, state="follow_up", deadline=at + timing.announce_follow_up_s), []
            if voice.state in ("thinking", "speaking"):
                if voice.ending:
                    return replace(voice, state="idle", deadline=None, ending=False), []
                return replace(voice, state="follow_up", deadline=at + timing.follow_up_s), []
            return voice, []
        case EndConversation():
            return replace(voice, ending=True), []
        case Tick(at):
            if voice.deadline is not None and at >= voice.deadline:
                return replace(voice, state="idle", deadline=None), []
            return voice, []


def _wake(voice: Voice, at: float, timing: Timing) -> tuple[Voice, list[Action]]:
    if at < voice.chime_until:
        return voice, []
    listening = replace(voice, state="listening", deadline=at + timing.listen_s, ending=False)
    if voice.state == "listening":
        return listening, []
    chimed = replace(listening, chime_until=at + timing.chime_s)
    if voice.state in ("thinking", "speaking", "announce"):
        return chimed, [Interrupt(), Chime()]
    return chimed, [Chime()]


def _heard(voice: Voice, text: str, lang: Lang | None, at: float, timing: Timing) -> tuple[Voice, list[Action]]:
    if voice.state not in CAPTURING:
        return voice, []
    turn = without_wake_phrase(text)
    if _is_noise(turn):
        return replace(voice, state="listening", deadline=at + timing.listen_s), []
    if (stop_lang := stop_phrase(turn)) is not None:
        return replace(voice, state="idle", deadline=None, ending=False), [Say("very_good", stop_lang)]
    return replace(voice, state="thinking", deadline=None), [SendTurn(turn, lang, at)]
