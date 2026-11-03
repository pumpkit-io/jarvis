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
class ChimeStarted:
    """The chime the Wake word asked for started playing."""

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
    """A reply's first audio started playing. `answers` is the `SendTurn.at` of the turn it answers, if any."""

    at: float
    answers: float | None = None


@dataclass(frozen=True)
class ReplyDone:
    """A reply has finished playing, or ended without any audio. `answers` as for `ReplyAudio`."""

    at: float
    answers: float | None = None


@dataclass(frozen=True)
class EndConversation:
    """The Talker called `end_conversation`: idle once the reply has played, with no Follow-up window."""

    at: float


@dataclass(frozen=True)
class Tick:
    at: float


# What the audio input reports.
InputEvent = WakeWord | SpeechStarted | Heard
Event = WakeWord | ChimeStarted | SpeechStarted | Heard | ReplyAudio | ReplyDone | EndConversation | Tick


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
    """`at`, when the turn was heard, also tags the reply begun for it."""

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
    # The reply in progress ends the conversation: idle once it has played.
    ending: bool = False
    # The User has started a turn that is not yet heard.
    in_turn: bool = False
    # When the Follow-up window the turn started in ends; it runs on through a turn of noise.
    window_ends: float | None = None
    # The `at` of the spoken turn sent and not yet answered: only its reply moves `thinking` on.
    awaiting: float | None = None
    # When the Wake word last opened listening: a spoken turn heard before it is one the User has moved on from.
    woke_at: float | None = None


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

# The wake phrase at the start of the turn: "Jarvis", after greetings if any.
_WAKE_PHRASE = re.compile(r"^\W*(?:(?:hey|hi|ehi|ehy|ok|okay)\W+)*jarvis\b(?!')\W*", re.IGNORECASE)
_WORDS = re.compile(r"[\w']+")


def _words(text: str) -> list[str]:
    return [w.lower().replace("’", "'") for w in _WORDS.findall(text.replace("’", "'"))]


# What is left of "Jarvis" when the Wake word fired before STT's end of the word: "Harvis", "Jarvid", "Jorvis".
_FIRST_WORD = re.compile(r"^(\w+)(?!')\W+(?=\w)")


def _sounds_like_jarvis(word: str) -> bool:
    return SequenceMatcher(None, word.lower(), "jarvis").ratio() >= 0.8


def without_wake_phrase(text: str) -> str:
    text = text.strip()
    if wake := _WAKE_PHRASE.match(text):
        return text[wake.end() :]
    if (first := _FIRST_WORD.match(text)) and _sounds_like_jarvis(first[1]):
        return text[first.end() :]
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
        case ChimeStarted(at):
            return replace(voice, chime_until=max(voice.chime_until, at + timing.chime_s)), []
        case SpeechStarted(at):
            if voice.state in CAPTURING and not voice.in_turn:
                ends = voice.deadline if voice.state == "follow_up" else None
                return replace(voice, state="listening", deadline=None, in_turn=True, window_ends=ends), []
            return voice, []
        case Heard(text, lang, at):
            return _heard(voice, text, lang, at, timing)
        case ReplyAudio(_, answers):
            if voice.state == "idle":
                return replace(voice, state="announce", deadline=None), []
            if voice.state == "follow_up" or (voice.state == "thinking" and answers == voice.awaiting):
                return replace(voice, state="speaking", deadline=None), []
            return voice, []
        case ReplyDone(at, answers):
            if voice.state not in ("thinking", "speaking", "announce"):
                return voice, []
            if voice.state == "thinking" and answers != voice.awaiting:
                return voice, []
            voice = replace(voice, awaiting=None)
            if voice.ending:
                return replace(voice, state="idle", deadline=None, ending=False), []
            window = timing.announce_follow_up_s if voice.state == "announce" else timing.follow_up_s
            return replace(voice, state="follow_up", deadline=at + window), []
        case EndConversation():
            if voice.state in ("thinking", "speaking", "announce"):
                return replace(voice, ending=True), []
            return voice, []
        case Tick(at):
            if voice.deadline is not None and at >= voice.deadline:
                return replace(voice, state="idle", deadline=None), []
            return voice, []


def _wake(voice: Voice, at: float, timing: Timing) -> tuple[Voice, list[Action]]:
    # A hit inside the User's turn is "Jarvis" said in it, or the phrase that opened it: the turn goes on.
    if at < voice.chime_until or voice.in_turn:
        return voice, []
    listening = replace(
        voice, state="listening", deadline=at + timing.listen_s, ending=False, awaiting=None, woke_at=at
    )
    if voice.state == "listening":
        return listening, []
    chimed = replace(listening, chime_until=at + timing.chime_s)
    if voice.state in ("thinking", "speaking", "announce"):
        return chimed, [Interrupt(), Chime()]
    return chimed, [Chime()]


def _heard(voice: Voice, text: str, lang: Lang | None, at: float, timing: Timing) -> tuple[Voice, list[Action]]:
    if voice.state not in CAPTURING:
        return voice, []
    heard = replace(voice, in_turn=False, window_ends=None)
    turn = without_wake_phrase(text)
    if _is_noise(turn):
        # Only the wake phrase, said in the window, waits for the request as the Wake word would.
        if voice.window_ends is not None and _is_noise(text):
            return replace(heard, state="follow_up", deadline=voice.window_ends), []
        return replace(heard, state="listening", deadline=at + timing.listen_s), []
    if (stop_lang := stop_phrase(turn)) is not None:
        return replace(heard, state="idle", deadline=None, ending=False), [Say("very_good", stop_lang)]
    return replace(heard, state="thinking", deadline=None, ending=False, awaiting=at), [SendTurn(turn, lang, at)]
