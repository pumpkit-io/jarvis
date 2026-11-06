"""The voice state machine: when Jarvis listens, replies and goes idle.

Pure: `step` takes the current `Voice`, one event stamped with its time, and the timing settings, and returns the
next `Voice` with the actions to carry out. Time passes only through events, `Tick` among them.
"""

import re
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Literal

from jarvis_hub.voice.bargein import Partial, decide, interrupts, still_talking
from jarvis_hub.voice.language import Lang

VoiceState = Literal["idle", "listening", "thinking", "speaking", "follow_up", "announce"]

# The states in which the User's speech becomes a turn.
CAPTURING: frozenset[VoiceState] = frozenset({"listening", "follow_up"})
# The states in which Jarvis's voice is heard, so the User's speech over it is a Barge-in.
VOICED: frozenset[VoiceState] = frozenset({"speaking", "announce"})


@dataclass(frozen=True)
class Timing:
    follow_up_s: float
    # After an Announcement, a shorter window than after a reply.
    announce_follow_up_s: float
    # After the Wake word, how long the User has to start speaking.
    listen_s: float
    # Wake word hits this soon after the chime starts are the chime's own echo or the same phrase, and are ignored.
    chime_s: float
    # From the pause, how long a Barge-in has to be told apart as a Backchannel or an Interruption.
    decide_s: float = 0.8
    # Past that deadline, how long to wait for one more partial when the User is still talking over Backchannels.
    last_partial_s: float = 0.5
    # From when Jarvis's voice starts or resumes, how long echo cancellation gets to settle before speech counts.
    guard_s: float = 0.25


@dataclass(frozen=True)
class WakeWord:
    at: float
    delivered_at: float | None = None
    owner: int | None = None
    idle_candidate: int | None = None


@dataclass(frozen=True)
class ChimeStarted:
    """The chime the Wake word asked for started playing."""

    at: float


@dataclass(frozen=True)
class SpeechStarted:
    at: float
    owner: int | None = None
    candidate: int | None = None
    started_at: float | None = None


@dataclass(frozen=True)
class CaptureAborted:
    """Captured speech was discarded before a request could be admitted."""

    at: float
    reason: str
    owner: int | None = None
    candidate: int | None = None
    wake_handoff: bool = False


@dataclass(frozen=True)
class BargeIn:
    """The User's speech over Jarvis has held long enough to pause it: it began at `onset`."""

    onset: float
    at: float


@dataclass(frozen=True)
class Overheard:
    """What the User said over Jarvis in a run that has made all the Barge-ins it may: only a hard interrupt counts."""

    text: str
    at: float


@dataclass(frozen=True)
class Heard:
    """The User's turn, transcribed once its end was detected. `lang` is None when the STT does not report it."""

    text: str
    lang: Lang | None
    at: float
    owner: int | None = None
    candidate: int | None = None
    started_at: float | None = None
    limited: bool = False
    delivered_at: float | None = None


@dataclass(frozen=True)
class ReplyAudio:
    """A reply's first audio started playing. `answers` is the `SendTurn.at` of the turn it answers, if any."""

    at: float
    answers: float | None = None
    owner: tuple[str, int] | None = None


@dataclass(frozen=True)
class ReplyDone:
    """A reply has finished playing, or ended without any audio. `answers` as for `ReplyAudio`."""

    at: float
    answers: float | None = None
    owner: tuple[str, int] | None = None


@dataclass(frozen=True)
class EndConversation:
    """The Talker called `end_conversation`: idle once the reply has played, with no Follow-up window."""

    at: float


@dataclass(frozen=True)
class AnnouncementBegins:
    """The Hub is starting news on the Talker, which it does only while idle: a chime, then the news, which a Wake
    word from now on interrupts."""

    at: float


@dataclass(frozen=True)
class Tick:
    at: float


# What the audio input reports.
InputEvent = WakeWord | SpeechStarted | CaptureAborted | Heard | BargeIn | Partial | Overheard
Event = (
    WakeWord
    | ChimeStarted
    | SpeechStarted
    | CaptureAborted
    | Heard
    | BargeIn
    | Partial
    | Overheard
    | ReplyAudio
    | ReplyDone
    | EndConversation
    | AnnouncementBegins
    | Tick
)


@dataclass(frozen=True)
class Chime:
    pass


@dataclass(frozen=True)
class SendTurn:
    """`at`, when the turn was heard, also tags the reply begun for it."""

    text: str
    lang: Lang | None
    at: float
    # The User cut Jarvis off before this turn: the Talker is told what they heard of it.
    interrupted: bool = False


@dataclass(frozen=True)
class Interrupt:
    """Stop the reply in progress, both the Talker's turn and its audio."""


@dataclass(frozen=True)
class Pause:
    """Hold Jarvis's voice where it is while a Barge-in is decided."""


@dataclass(frozen=True)
class Resume:
    """Go on with Jarvis's voice from a little before the pause."""


Action = Chime | SendTurn | Interrupt | Pause | Resume


@dataclass(frozen=True)
class Barge:
    """A Barge-in being decided: Jarvis is paused, and each partial transcript may settle it."""

    onset: float
    partials: tuple[Partial, ...] = ()
    # The deadline has passed with the User still talking: the next partial is the last.
    last_call: bool = False


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
    # When Jarvis's voice last started or resumed, while it is heard; None otherwise.
    voiced_at: float | None = None
    barge: Barge | None = None
    # The User cut Jarvis off and their next turn has not been sent yet.
    cut_off: bool = False


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


def barge_in_from(voice: Voice, timing: Timing) -> float | None:
    """From when the User's speech over Jarvis counts toward a Barge-in, or None while Jarvis's voice is not heard.

    The audio input applies it, measuring speech from here, so speech already going at that time still counts.
    """
    if voice.state not in VOICED or voice.voiced_at is None:
        return None
    return voice.voiced_at + timing.guard_s


def step(voice: Voice, event: Event, timing: Timing) -> tuple[Voice, list[Action]]:
    after, actions = _step(voice, event, timing)
    if after.state not in VOICED:
        after = replace(after, voiced_at=None, barge=None)
    if after.state == "idle":
        after = replace(after, cut_off=False)
    return after, actions


def _step(voice: Voice, event: Event, timing: Timing) -> tuple[Voice, list[Action]]:
    match event:
        case WakeWord(_, _, _, idle_candidate) if idle_candidate is not None and voice.state != "idle":
            return voice, []
        case WakeWord(at, delivered_at):
            return _wake(voice, at, timing, delivered_at)
        case ChimeStarted(at):
            return replace(voice, chime_until=max(voice.chime_until, at + timing.chime_s)), []
        case SpeechStarted(at):
            if voice.state in CAPTURING and not voice.in_turn:
                ends = voice.deadline if voice.state == "follow_up" else None
                return replace(voice, state="listening", deadline=None, in_turn=True, window_ends=ends), []
            return voice, []
        case Heard(text, lang, at):
            return _heard(voice, text, lang, at, timing, event.delivered_at)
        case CaptureAborted(_, _, _, _, True) if voice.state == "listening":
            return replace(voice, state="idle", deadline=None, in_turn=False, window_ends=None), []
        case CaptureAborted(at):
            if voice.state in CAPTURING and voice.in_turn:
                return _heard(voice, "", None, at, timing)
            return voice, []
        case ReplyAudio(at, answers):
            # Only a spoken turn's reply moves on here: news has already claimed the voice with AnnouncementBegins.
            if voice.state == "thinking" and answers == voice.awaiting:
                return replace(voice, state="speaking", deadline=None, voiced_at=at), []
            if voice.state == "announce" and voice.barge is None:
                return replace(voice, voiced_at=at), []
            return voice, []
        case ReplyDone(at, answers):
            if voice.state not in ("thinking", "speaking", "announce"):
                return voice, []
            if voice.state == "thinking" and answers != voice.awaiting:
                return voice, []
            # Nothing is left to resume, but the player must not stay paused for the next reply.
            unpause: list[Action] = [Resume()] if voice.barge is not None else []
            voice = replace(voice, awaiting=None)
            if voice.ending:
                return replace(voice, state="idle", deadline=None, ending=False), unpause
            window = timing.announce_follow_up_s if voice.state == "announce" else timing.follow_up_s
            return replace(voice, state="follow_up", deadline=at + window), unpause
        case BargeIn(onset, at):
            if barge_in_from(voice, timing) is None or voice.barge is not None:
                return voice, []
            return replace(voice, barge=Barge(onset), deadline=at + timing.decide_s), [Pause()]
        case Partial(_, at):
            if voice.barge is None:
                return voice, []
            barge = replace(voice.barge, partials=(*voice.barge.partials, event))
            return _decide(replace(voice, barge=barge), at, timing, final=barge.last_call)
        case Overheard(text, at):
            if voice.state in VOICED and voice.barge is None and interrupts(text):
                return _stop(voice, at, timing)
            return voice, []
        case EndConversation():
            if voice.state in ("thinking", "speaking", "announce"):
                return replace(voice, ending=True), []
            return voice, []
        case AnnouncementBegins():
            if voice.state == "idle":
                return replace(voice, state="announce", deadline=None), [Chime()]
            return voice, []
        case Tick(at):
            if (barge := voice.barge) is not None:
                if voice.deadline is None or at < voice.deadline:
                    return voice, []
                # Backchannels so far may lead into more ("okay, actually…"): hear what follows before resuming.
                talking = bool(barge.partials) and still_talking(barge.partials[-1])
                if not barge.last_call and talking and decide(barge.partials, final=True) == "resume":
                    last_call = replace(barge, last_call=True)
                    return replace(voice, barge=last_call, deadline=at + timing.last_partial_s), []
                return _decide(voice, at, timing, final=True)
            if voice.deadline is not None and at >= voice.deadline:
                return replace(voice, state="idle", deadline=None), []
            return voice, []


def _decide(voice: Voice, at: float, timing: Timing, *, final: bool) -> tuple[Voice, list[Action]]:
    assert voice.barge is not None
    match decide(voice.barge.partials, final=final):
        case "wait":
            return voice, []
        case "resume":
            return replace(voice, barge=None, deadline=None, voiced_at=at), [Resume()]
        case "stop":
            return _stop(voice, at, timing)


def _stop(voice: Voice, at: float, timing: Timing) -> tuple[Voice, list[Action]]:
    listening = replace(
        voice, state="listening", deadline=at + timing.listen_s, ending=False, awaiting=None, cut_off=True
    )
    return listening, [Interrupt()]


def _wake(voice: Voice, at: float, timing: Timing, delivered_at: float | None = None) -> tuple[Voice, list[Action]]:
    delivered = at if delivered_at is None else delivered_at
    # A hit inside the User's turn is "Jarvis" said in it, or the phrase that opened it: the turn goes on.
    if delivered < voice.chime_until or voice.in_turn:
        return voice, []
    listening = replace(
        voice,
        state="listening",
        deadline=delivered + timing.listen_s,
        ending=False,
        awaiting=None,
        woke_at=at,
        cut_off=voice.cut_off or voice.state in VOICED,
    )
    if voice.state == "listening":
        return listening, []
    chimed = replace(listening, chime_until=delivered + timing.chime_s)
    if voice.state in ("thinking", "speaking", "announce"):
        return chimed, [Interrupt(), Chime()]
    return chimed, [Chime()]


def _heard(voice: Voice, text: str, lang: Lang | None, at: float, timing: Timing, delivered_at: float | None = None) -> tuple[Voice, list[Action]]:
    if voice.state not in CAPTURING:
        return voice, []
    heard = replace(voice, in_turn=False, window_ends=None)
    turn = without_wake_phrase(text)
    if _is_noise(turn):
        # Noise in the Follow-up window lets the window run on to its end.
        if voice.window_ends is not None and _is_noise(text):
            return replace(heard, state="follow_up", deadline=voice.window_ends), []
        # The wake phrase alone, or noise after the Wake word, waits for the request as the Wake word would.
        return replace(heard, state="listening", deadline=(at if delivered_at is None else delivered_at) + timing.listen_s), []
    if stop_phrase(turn) is not None:
        return replace(heard, state="idle", deadline=None, ending=False), []
    sent = SendTurn(turn, lang, at, interrupted=voice.cut_off)
    return replace(heard, state="thinking", deadline=None, ending=False, awaiting=at, cut_off=False), [sent]
