"""Voice mode: runs the voice state machine on the audio input's events and the replies' playback.

The User's turns come out as `Line`s for the Hub; the Hub's spoken replies come back through `speech`, so the
machine knows when a reply starts and ends playing.
"""

import asyncio
import contextlib
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol
from uuid import uuid4

from jarvis_hub.hub import Line
from jarvis_hub.voice.assessment import Assessment, Candidate
from jarvis_hub.voice.followup import FollowUpWindow
from jarvis_hub.voice.ownership import MAX_REQUEST_CHARACTERS, SpokenTurn
from jarvis_hub.voice.bargein import Partial
from jarvis_hub.voice.language import Lang
from jarvis_hub.voice.player import Player
from jarvis_hub.voice.speech import ClipName, Metrics, Speech, SpeechClock
from jarvis_hub.voice.state import (
    CAPTURING,
    VOICED,
    BargeIn,
    Overheard,
    Action,
    AnnouncementBegins,
    Chime,
    ChimeStarted,
    CaptureAborted,
    EndConversation,
    Event,
    InputEvent,
    Heard,
    SpeechStarted,
    WakeWord,
    Interrupt,
    Pause,
    ReplyAudio,
    ReplyDone,
    Resume,
    SendTurn,
    Tick,
    Timing,
    Voice,
    VoiceState,
    step,
    stop_phrase,
)
from jarvis_hub.voice.tts import Synthesizer


class AudioInput(Protocol):
    """What the input pipeline reports (`jarvis_hub.voice.input.Capture`), or a test's script."""

    def events(self) -> AsyncIterator[InputEvent]: ...

    def capture(self, on: bool, *, owner: int | None = None, idle: bool = False, wake_candidate: int | None = None) -> bool:
        """`idle` grants keyword proposals; a stale retained wake handoff returns False."""
        ...

    def barge_in(self, since: float | None) -> None:
        """Legacy partial replay control; contextual playback admission always disables it with None."""
        ...


class RelationAssessment(Protocol):
    async def assess(
        self, candidate: Candidate, *, current: Callable[[], tuple[str, str]], timeout_s: float = 2.0,
    ) -> Assessment: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class _Fragment:
    heard: Heard
    phase: str
    before_reply: bool | None


@dataclass(frozen=True)
class _Related:
    candidate: Candidate
    fragments: tuple[_Fragment, ...]
    owner: SpokenTurn
    epoch: int
    result: Assessment

    @property
    def heard(self) -> Heard:
        return self.fragments[-1].heard


@dataclass(frozen=True)
class _FollowUpRelated:
    window: FollowUpWindow
    fragments: tuple[Heard, ...]
    candidate: Candidate
    version: int
    result: Assessment


class _ObservedSpeech:
    """The Hub's speech, telling the machine when each reply has finished playing.

    The answer to a turn heard before the latest Wake word is dropped: the User moved on, and it stays on screen.
    """

    def __init__(
        self,
        speech: Speech,
        done: Callable[[float | None, tuple[str, int] | None], Awaitable[None]],
        moved_on: Callable[[float | None, tuple[str, int] | None], bool],
    ) -> None:
        self._speech = speech
        self._done = done
        self._moved_on = moved_on
        self._replies = 0
        # Replies up to this one were stopped: their playback ending is not a reply done.
        self._stopped = 0
        self._heard_at: float | None = None
        self._owner: tuple[str, int] | None = None
        self._dropping = False
        self._waits: set[asyncio.Task[None]] = set()

    def begin(self, lang: Lang, *, turn: int, heard_at: float | None, owner: tuple[str, int] | None = None) -> None:
        self._dropping = self._moved_on(heard_at, owner)
        if self._dropping:
            return
        self._replies += 1
        self._heard_at = heard_at
        self._owner = owner
        self._speech.begin(lang, turn=turn, heard_at=heard_at, owner=owner)

    def feed(self, text: str) -> None:
        if not self._dropping:
            self._speech.feed(text)

    def say(self, name: ClipName, lang: Lang, *, in_reply: bool = False) -> None:
        if not self._dropping:
            self._speech.say(name, lang, in_reply=in_reply)

    def block_end(self) -> None:
        if not self._dropping:
            self._speech.block_end()

    def end(self) -> None:
        if self._dropping:
            self._dropping = False
            return
        self._speech.end()
        wait = asyncio.create_task(self._played(self._replies, self._heard_at, self._owner))
        self._waits.add(wait)
        wait.add_done_callback(self._waits.discard)

    def stop(self) -> None:
        self._dropping = True
        self._stopped = self._replies
        self._speech.stop()

    async def _played(self, reply: int, heard_at: float | None, owner: tuple[str, int] | None) -> None:
        await self._speech.idle()
        # A reply begun since has the floor; its own end reports.
        if reply == self._replies and reply > self._stopped:
            await self._done(heard_at, owner)


class VoiceMode:
    def __init__(
        self,
        *,
        audio: AudioInput,
        tts: Synthesizer,
        player: Player,
        clock: SpeechClock,
        timing: Timing,
        metrics: Metrics,
        notice: Callable[[str], None],
        debug: bool = False,
        recognition: RelationAssessment | None = None,
    ) -> None:
        self._audio = audio
        self._player = player
        self._clock = clock
        self._timing = timing
        self._metrics = metrics
        self._notice = notice
        self._debug = debug
        self._voice: Voice = Voice()
        self._recognition = recognition
        self._epoch = 0
        self._request_sequence = 0
        self._request_namespace = uuid4().hex
        self._admitted: Callable[[Line], None] = lambda line: None
        self._owned: SpokenTurn | None = None
        self._reply_started: tuple[tuple[str, int], float] | None = None
        self._reply_finished: tuple[tuple[str, int], float] | None = None
        self._closed = False
        self._typed_at = float("-inf")
        self._follow_up: FollowUpWindow | None = None
        self._follow_up_query: asyncio.Task[None] | None = None
        self._capture_setting: tuple[bool, int, bool] | None = None
        self._wake_candidate: int | None = None
        self._origins: dict[int | None, tuple[str, bool | None]] = {}
        self._owned_capture_active: set[int | None] = set()
        self._rejected_sources: set[int] = set()
        self._reject_unknown_sources = False
        self._waiting: deque[_Fragment] = deque()
        self._retained: deque[_Fragment] = deque()
        self._inflight: tuple[_Fragment, ...] = ()
        self._applying_fragments: tuple[_Fragment, ...] = ()
        self._latest_candidate = -1
        self._accepted_candidate = -1
        self._assessing: asyncio.Task[None] | None = None
        self._applying: set[asyncio.Task[None]] = set()
        # What the User heard of the voice they last cut off, for their next turn.
        self._heard_before_cut = ""
        self._idle = asyncio.Event()
        self._idle.set()
        self._woke: Callable[[], None] = lambda: None
        self._events: asyncio.Queue[Event | _Related | _FollowUpRelated] = asyncio.Queue()
        self._lines: asyncio.Queue[Line] = asyncio.Queue()
        self._speech = Speech(
            tts, player, clock, metrics=metrics, notice=notice, on_reply_audio=self._reply_audio, on_chime=self._chime_started, on_ready=self._ready
        )
        self.speech = _ObservedSpeech(self._speech, self._reply_done, self._moved_on)

    @property
    def state(self) -> VoiceState:
        return self._voice.state

    def start(self) -> None:
        self._audio.barge_in(None)
        self._notice("Speech loading.")
        self._speech.start()

    def _ready(self) -> None:
        self._notice("Speech ready.")
        self._speech.say("ready", "en")

    def end_conversation(self) -> None:
        self._events.put_nowait(EndConversation(self._clock.now()))

    async def lines(
        self, interrupt: Callable[[], Awaitable[Any]], woke: Callable[[], None] = lambda: None,
        admitted: Callable[[Line], None] = lambda line: None,
    ) -> AsyncIterator[Line]:
        """The User's spoken turns, for as long as this is iterated. `woke` hears of each Wake word that opens
        listening, before the turn that follows it."""
        self._woke = woke
        self._admitted = admitted
        tasks = [asyncio.create_task(self._listen()), asyncio.create_task(self._drive(interrupt))]
        try:
            while True:
                yield await self._lines.get()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.wait(tasks)

    async def idle(self) -> None:
        await self._speech.idle()

    async def quiet(self) -> None:
        """Returns once the machine is idle and queued audio has drained."""
        while True:
            await self._idle.wait()
            await self._speech.idle()
            if self._voice.state == "idle" and not self._follow_up_pending():
                return

    def _follow_up_pending(self) -> bool:
        window = self._follow_up
        return window is not None and bool(window.active or window.waiting or window.inflight or self._owned_capture_pending())

    def _owned_capture_pending(self) -> bool:
        return bool(self._owned_capture_active or self._waiting or self._inflight or self._applying_fragments or self._applying)

    def _clear_owned_fragments(self) -> None:
        self._owned_capture_active.clear()
        self._waiting.clear()
        self._retained.clear()
        self._inflight = ()
        self._applying_fragments = ()
        self._origins.clear()

    def _known_pending_source(self, candidate: int | None) -> bool:
        if candidate is None:
            return False
        window = self._follow_up
        fragments = (*self._retained, *self._inflight, *self._applying_fragments, *self._waiting)
        return (candidate in self._owned_capture_active or any(fragment.heard.candidate == candidate for fragment in fragments)
                or window is not None and (candidate in window.active or any(fragment.candidate == candidate
                    for fragment in (*window.retained, *window.inflight, *window.waiting))))

    def _reject_source(self, candidate: int | None) -> None:
        if candidate is not None and (candidate in self._rejected_sources or len(self._rejected_sources) < 64):
            self._rejected_sources.add(candidate)
        elif not self._reject_unknown_sources:
            self._reject_unknown_sources = True
            self._notice("Speech source tracking is unavailable. Use a fresh wake once Jarvis is idle.")

    def _settle_expired_source(self) -> None:
        if self._voice.state == "idle" and self._follow_up is not None and not self._owned_capture_pending():
            self._retire(preserve_follow_up=True)
        self._settle_idle()
        self._sync_capture()

    def _settle_idle(self) -> None:
        if self._voice.state == "idle" and not self._follow_up_pending():
            self._idle.set()
        else:
            self._idle.clear()

    def announce(self) -> bool:
        """Claim idle playback for news; captured speech still needs contextual admission."""
        if self._voice.state != "idle" or self._follow_up_pending():
            return False
        self._retire()
        began = self._clock.now()
        actions = self._step(AnnouncementBegins(began))
        self._follow_up = FollowUpWindow(f"announcement-{self._request_namespace}-{began}", self._epoch,
                                        began, float("inf"), "", "", None, phase="announce")
        for action in actions:
            if isinstance(action, Chime):
                # Queued now, so it plays ahead of the news turn's first sentence.
                self._speech.say("chime", "en")
        return True

    async def close(self) -> None:
        self._closed = True
        self._retire()
        pending = [task for task in [self._assessing, self._follow_up_query, *self._applying] if task is not None]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending)
        if self._recognition is not None:
            await self._recognition.close()
        await self._speech.close()

    def _reply_audio(self, heard_at: float | None, owner: tuple[str, int] | None = None) -> None:
        now = self._clock.now()
        if owner is not None and self.owns(owner):
            self._reply_started = (owner, now)
        self._events.put_nowait(ReplyAudio(now, heard_at, owner))

    def _chime_started(self, at: float) -> None:
        self._events.put_nowait(ChimeStarted(at))

    async def _reply_done(self, heard_at: float | None, owner: tuple[str, int] | None = None) -> None:
        now = self._clock.now()
        if owner is not None and self.owns(owner):
            self._reply_finished = (owner, now)
        self._events.put_nowait(ReplyDone(now, heard_at, owner))

    def _moved_on(self, heard_at: float | None, owner: tuple[str, int] | None = None) -> bool:
        if owner is not None and not self.owns(owner):
            return True
        woke_at = self._voice.woke_at
        return heard_at is not None and woke_at is not None and heard_at < woke_at

    async def _listen(self) -> None:
        async for event in self._audio.events():
            self._events.put_nowait(event)

    async def _drive(self, interrupt: Callable[[], Awaitable[Any]]) -> None:
        while True:
            event = await self._next_event()
            if isinstance(event, (BargeIn, Partial, Overheard)):
                continue
            if isinstance(event, WakeWord) and (self._voice.state != "idle" or self._follow_up_pending()):
                continue
            if isinstance(event, _FollowUpRelated):
                self._follow_up_query = None
                await self._apply_follow_up(event, interrupt)
                self._start_follow_up_assessment()
                continue
            if (isinstance(event, WakeWord) and self._follow_up is not None and self._follow_up.includes(event.at)
                    and (event.at < self._follow_up.expires or self._voice.state != "idle")):
                continue
            if isinstance(event, _Related):
                if event.fragments is self._inflight and self.owns(event.owner.token) and event.epoch == self._epoch:
                    self._applying_fragments = event.fragments
                    applying = asyncio.create_task(self._apply_related(event, interrupt))
                    self._applying.add(applying)
                    applying.add_done_callback(self._applied)
                self._assessing = None
                self._inflight = ()
                if not self._applying:
                    self._start_assessment()
                continue
            if isinstance(event, CaptureAborted) and event.wake_handoff and event.owner != self._epoch:
                continue
            if isinstance(event, (Heard, SpeechStarted, CaptureAborted)):
                if event.owner is not None and event.owner != self._epoch:
                    continue
                if (
                    event.candidate in self._rejected_sources
                    or self._reject_unknown_sources and not self._known_pending_source(event.candidate)
                ):
                    continue
                if event.candidate is not None:
                    if event.candidate <= self._accepted_candidate:
                        continue
            if isinstance(event, Heard):
                self._owned_capture_active.discard(event.candidate)
            if isinstance(event, (SpeechStarted, Heard)) and self._follow_up_event(event):
                continue
            if isinstance(event, (SpeechStarted, Heard)) and event.candidate is not None:
                self._latest_candidate = max(self._latest_candidate, event.candidate)
            if isinstance(event, CaptureAborted) and self._follow_up is not None:
                window = self._follow_up
                tracked = window.active | {fragment.candidate for fragment in (*window.retained, *window.waiting, *window.inflight)}
                tracked |= set(self._origins) | {fragment.heard.candidate for fragment in (*self._retained, *self._inflight, *self._applying_fragments, *self._waiting)}
                if event.candidate not in tracked:
                    continue
                window.active.clear()
                window.retained, window.waiting, window.inflight = (), [], ()
                window.version += 1
                self._clear_owned_fragments()
                self._accepted_candidate = max(self._accepted_candidate, event.candidate if event.candidate is not None else -1)
                self._restore_follow_up(window)
                continue
            if isinstance(event, CaptureAborted):
                self._clear_owned_fragments()
                if event.candidate is not None:
                    self._accepted_candidate = max(self._accepted_candidate, event.candidate)
                if self._owned is not None and self._voice.state == "thinking":
                    self._notice("That additional speech was lost. Say 'Hey Jarvis' and try again.")
            if isinstance(event, Heard) and event.limited:
                self._notice("Spoken input reached its duration limit. Repeat or split the request.")
                self._clear_owned_fragments()
                if self._follow_up is not None:
                    self._finish_follow_up(self._follow_up)
                elif self._voice.state in CAPTURING:
                    before = self._voice.state
                    self._voice = Voice()
                    self._retire()
                    self._transition(before, "idle", event)
                continue
            if isinstance(event, SpeechStarted):
                if len(self._origins) >= 64:
                    oldest = next(iter(self._origins))
                    self._origins.pop(oldest)
                    self._owned_capture_active.discard(oldest)
                self._origins[event.candidate] = self._origin(event.started_at if event.started_at is not None else event.at)
                if self._origins[event.candidate][0] in ("thinking", "speaking"):
                    self._owned_capture_active.add(event.candidate)
            if isinstance(event, Heard) and self._owned is not None:
                origin = self._origins.get(event.candidate, self._origin(event.started_at))
                if origin[0] in ("thinking", "speaking") or self._retained or self._inflight or self._applying_fragments or self._waiting:
                    buffered = [*self._retained, *self._inflight, *self._applying_fragments, *self._waiting]
                    text = " ".join([self._owned.text, *(fragment.heard.text for fragment in buffered), event.text])
                    if len(buffered) >= 4 or len(text) > MAX_REQUEST_CHARACTERS:
                        self._notice("Spoken input reached its retention limit. Repeat or split the request.")
                        self._clear_owned_fragments()
                        self._settle_expired_source()
                    else:
                        self._waiting.append(_Fragment(event, *origin))
                        self._start_assessment()
                    continue
            for action in self._step(event):
                await self._act(action, interrupt)

    def _follow_up_event(self, event: SpeechStarted | Heard) -> bool:
        window = self._follow_up
        if window is None:
            return False
        onset = event.started_at
        if onset is None:
            onset = window.onsets.get(event.candidate, event.at)
        if onset < window.opened:
            origin = self._origins.get(event.candidate, self._origin(onset))
            owned_pending = self._retained or self._inflight or self._applying_fragments or self._waiting
            return not (self._owned is not None and (origin[0] in ("thinking", "speaking") or owned_pending))
        if not window.includes(onset):
            self._reject_source(event.candidate)
            return True
        if isinstance(event, SpeechStarted):
            if len(window.onsets) >= 64:
                oldest = next(iter(window.onsets))
                window.onsets.pop(oldest)
                window.active.discard(oldest)
            window.onsets[event.candidate] = onset
            window.active.add(event.candidate)
            self._idle.clear()
            if event.candidate is not None and (self._retained or self._inflight or self._applying_fragments or self._waiting):
                self._latest_candidate = max(self._latest_candidate, event.candidate)
            window.version += 1
            if self._clock.now() < window.expires:
                self._voice = replace(self._voice, in_turn=True)
            return True
        window.active.discard(event.candidate)
        if not event.limited and (self._retained or self._inflight or self._applying_fragments or self._waiting):
            return False
        if event.limited:
            self._notice("Spoken input reached its duration limit. Repeat or split the request.")
            self._finish_follow_up(window, keep_playback=True)
            return True
        if not event.text.strip() or self._recognition is None:
            self._restore_follow_up(window)
            return True
        buffered = (*window.retained, *window.inflight, *window.waiting, event)
        if len(buffered) > 4 or len(" ".join(fragment.text for fragment in buffered)) > MAX_REQUEST_CHARACTERS:
            self._notice("Spoken input reached its retention limit. Repeat or split the request.")
            self._finish_follow_up(window, keep_playback=True)
            return True
        window.waiting.append(event)
        self._start_follow_up_assessment()
        return True

    def _restore_follow_up(self, window: FollowUpWindow) -> None:
        if self._follow_up is not window:
            return
        if self._voice.state == "announce" and window.phase == "announce":
            self._voice = replace(self._voice, in_turn=bool(window.active))
            self._sync_capture()
            return
        before = self._voice.state
        expired = self._clock.now() >= window.expires
        self._voice = replace(self._voice, state="idle" if expired else "follow_up", in_turn=False,
                              window_ends=None, deadline=None if expired else window.expires)
        if expired:
            if not window.active and not window.waiting:
                window.retained = ()
            self._retire(preserve_follow_up=True)
        if self._voice.state != before:
            self._transition(before, self._voice.state, Tick(self._clock.now()))
        self._settle_idle()
        self._sync_capture()

    def _finish_follow_up(self, window: FollowUpWindow, *, keep_playback: bool = False) -> None:
        if self._follow_up is not window:
            return
        if keep_playback and self._voice.state == "announce":
            window.retained, window.waiting, window.inflight = (), [], ()
            window.active.clear()
            window.version += 1
            self._restore_follow_up(window)
            return
        before = self._voice.state
        self._voice = Voice()
        self._retire()
        if before != "idle":
            self._transition(before, "idle", Tick(self._clock.now()))
        self._idle.set()

    def _start_follow_up_assessment(self) -> None:
        window = self._follow_up
        if window is None or not window.waiting or self._follow_up_query is not None or self._recognition is None:
            return
        fragments = (*window.retained, *window.waiting)
        window.retained, window.waiting, window.inflight = (), [], fragments
        version = window.version
        last = fragments[-1]
        first = fragments[0]
        onset = first.started_at if first.started_at is not None else window.onsets.get(first.candidate, first.at)
        phase = window.phase if window.completed_at is None or onset < window.completed_at else "follow_up"
        played = window.played_text
        if self._voice.state == "announce":
            played = self._speech.played().strip() if self._voice.voiced_at is not None else ""
        candidate = Candidate(f"{window.token}:{last.candidate}:{last.at}", window.token,
                              " ".join(fragment.text for fragment in fragments), window.admitted_turn,
                              played, phase, None if phase == "announce" else False,
                              tuple(fragment.text for fragment in fragments))
        def current() -> tuple[str, str]:
            return (candidate.candidate_id, candidate.turn_id) if self._follow_up is window and self._epoch == window.epoch and window.inflight is fragments and not self._closed else ("", "")
        async def assess() -> None:
            assert self._recognition is not None
            try:
                result = await self._recognition.assess(candidate, current=current)
            except Exception:
                result = Assessment(candidate.candidate_id, candidate.turn_id, "uncertain", "unavailable", 0)
            self._events.put_nowait(_FollowUpRelated(window, fragments, candidate, version, result))
        self._follow_up_query = asyncio.create_task(assess())

    async def _apply_follow_up(self, related: _FollowUpRelated, interrupt: Callable[[], Awaitable[Any]]) -> None:
        window, result = related.window, related.result
        if self._follow_up is not window or self._epoch != window.epoch or window.inflight is not related.fragments or self._closed:
            return
        window.inflight = ()
        actions = result.segment_actions
        if (result.candidate_id, result.turn_id) != (related.candidate.candidate_id, related.candidate.turn_id):
            actions = None
        valid = actions is not None and len(actions) == len(related.fragments) and all(action in ("floor", "stop", "ignore", "uncertain") for action in actions)
        if not valid:
            actions = tuple("uncertain" for _ in related.fragments)
        assert actions is not None
        self._metrics.record({"kind": "follow_up_admission", "window": window.token,
                              "phase": related.candidate.phase, "played_basis": "queue_estimate",
                              "reason": result.reason if valid else "invalid", "elapsed_ms": result.elapsed_ms,
                              "observed_segments": len(related.fragments), "selected_segments": [index for index, action in enumerate(actions) if action == "floor"]})
        if window.version != related.version or window.waiting:
            window.retained = tuple(fragment for fragment, action in zip(related.fragments, actions) if action != "ignore")
            if not window.active and not window.waiting:
                self._restore_follow_up(window)
            return
        selected = tuple(fragment for fragment, action in zip(related.fragments, actions) if action == "floor")
        if selected:
            text = " ".join(fragment.text for fragment in selected).strip()
            if related.candidate.phase == "follow_up" and self._voice.state in ("follow_up", "idle") and stop_phrase(text) is not None:
                self._finish_follow_up(window)
                return
            last = selected[-1]
            before_voice = self._voice
            before = before_voice.state
            segments = tuple(fragment.text for fragment in selected)
            if window.source is not None:
                try:
                    owned = window.source.amended(text, last.at, last.lang, segments=segments)
                except ValueError as error:
                    self._notice(str(error))
                    self._finish_follow_up(window)
                    return
            else:
                self._request_sequence += 1
                owned = SpokenTurn(f"spoken-{self._request_namespace}-{self._request_sequence}", 0, text,
                                   last.at, last.lang, segments)
            self._retire()
            self._owned = owned
            self._accepted_candidate = last.candidate if last.candidate is not None else -1
            self._voice = Voice(state="thinking", awaiting=last.at)
            self._transition(before, "thinking", last)
            self._sync_capture()
            played = self._speech.played().strip() if before == "announce" and before_voice.voiced_at is not None else None
            line = Line(owned.text, last.at, owned.lang, typed=False, request_id=owned.id,
                        revision=owned.revision, segments=owned.segments, interrupted=played)
            self._admitted(line)
            if before == "announce":
                self._record_stop_request(related.candidate, selected, "floor")
                await interrupt()
            if self.owns(owned.token):
                self._lines.put_nowait(line)
        elif "stop" in actions:
            playing = self._voice.state == "announce"
            if playing:
                stopped = tuple(fragment for fragment, action in zip(related.fragments, actions) if action == "stop")
                self._record_stop_request(related.candidate, stopped, "stop")
            if playing:
                played = self._speech.played().strip() if self._voice.voiced_at is not None else ""
                self._after_voice_stop(None, played, related.fragments[-1])
                await interrupt()
            else:
                self._finish_follow_up(window)
        else:
            window.retained = tuple(fragment for fragment, action in zip(related.fragments, actions) if action == "uncertain")
            self._restore_follow_up(window)

    def _origin(self, at: float | None) -> tuple[str, bool | None]:
        if self._owned is not None and at is not None:
            if self._reply_started is not None and self._reply_started[0] == self._owned.token:
                if at < self._reply_started[1]:
                    return "thinking", True
                finished = self._reply_finished
                if finished is not None and finished[0] == self._owned.token and at >= finished[1]:
                    return "follow_up", False
                return "speaking", False
            finished = self._reply_finished
            if finished is not None and finished[0] == self._owned.token:
                return ("thinking", True) if at < finished[1] else ("follow_up", None)
        return self._voice.state, True if self._voice.state == "thinking" else None

    def owns(self, owner: tuple[str, int]) -> bool:
        return not self._closed and self._owned is not None and self._owned.token == owner

    def _retire(self, *, preserve_follow_up: bool = False) -> bool:
        # Expiry ends eligibility for new onsets, not an already captured continuation.
        if preserve_follow_up and self._owned_capture_pending():
            return self._sync_capture()
        if not preserve_follow_up:
            self._epoch += 1
            self._follow_up = None
            self._rejected_sources.clear()
            self._reject_unknown_sources = False
        self._owned = None
        self._reply_started = None
        self._reply_finished = None
        self._accepted_candidate = -1
        self._clear_owned_fragments()
        self._latest_candidate = -1
        return self._sync_capture()

    def semantic_context(self, owner: tuple[str, int], start: int) -> None:
        if self.owns(owner) and self._owned is not None and 0 <= start < len(self._owned.segments):
            self._owned = replace(self._owned, semantic_start=start)

    def typed_input(self) -> None:
        self._typed_at = self._clock.now()
        # STT can hold an initial recording before its queued onset reaches this state machine.
        if self._owned is None and self._follow_up is None and self._voice.state in CAPTURING:
            return
        before = self._voice.state
        self._voice = Voice()
        self._retire()
        if before != "idle":
            self._transition(before, "idle", EndConversation(self._clock.now()))
        self._idle.set()

    def _sync_capture(self) -> bool:
        pending = self._follow_up_pending()
        setting = (not self._closed and (self._voice.state in CAPTURING or self._voice.state in VOICED
                                        or self._voice.state == "thinking" or pending),
                   self._epoch, not self._closed and self._voice.state == "idle" and not pending)
        if setting != self._capture_setting:
            self._capture_setting = setting
            accepted = self._audio.capture(setting[0], owner=setting[1], idle=setting[2], wake_candidate=self._wake_candidate)
            self._wake_candidate = None
            return accepted
        return True

    def _applied(self, task: asyncio.Task[None]) -> None:
        self._applying.discard(task)
        self._applying_fragments = ()
        self._start_assessment()
        self._settle_expired_source()

    def _start_assessment(self) -> None:
        if self._assessing is not None or self._applying or not self._waiting or self._owned is None or self._closed:
            return
        fragments = tuple([*self._retained, *self._waiting])
        self._retained.clear()
        self._waiting.clear()
        self._inflight = fragments
        heard = fragments[-1].heard
        owner, epoch = self._owned, self._epoch
        first = fragments[0]
        transcript = " ".join(fragment.heard.text for fragment in fragments).strip()
        candidate = Candidate(f"{epoch}:{heard.candidate}:{heard.at}", f"{owner.id}:{owner.revision}",
                              transcript, owner.semantic_text, self._speech.played(), first.phase, first.before_reply,
                              tuple(fragment.heard.text for fragment in fragments))

        def current() -> tuple[str, str]:
            return (candidate.candidate_id, candidate.turn_id) if self.owns(owner.token) and epoch == self._epoch and self._inflight is fragments else ("", "")

        async def assess() -> None:
            if self._recognition is None:
                result = Assessment(candidate.candidate_id, candidate.turn_id, "uncertain", "no_local_recognizer", 0)
            else:
                try:
                    result = await self._recognition.assess(candidate, current=current)
                except Exception:
                    result = Assessment(candidate.candidate_id, candidate.turn_id, "uncertain", "unavailable", 0)
            self._events.put_nowait(_Related(candidate, fragments, owner, epoch, result))
        self._assessing = asyncio.create_task(assess())

    def _after_voice_stop(self, source: SpokenTurn | None, played: str, heard: Heard) -> None:
        before = self._voice.state
        opened = self._clock.now()
        expires = opened + self._timing.listen_s
        self._voice = Voice(state="follow_up", deadline=expires)
        self._retire()
        self._follow_up = FollowUpWindow(f"voice-stop-{self._request_namespace}-{opened}", self._epoch,
                                        opened, expires, source.semantic_text if source is not None else "",
                                        played, source)
        self._transition(before, "follow_up", heard)
        self._settle_idle()

    def _record_stop_request(self, candidate: Candidate, fragments: tuple[Heard, ...], decision: str) -> None:
        onset = fragments[0].started_at if fragments else None
        requested = self._clock.now()
        self._metrics.record({
            "kind": "playback_admission", "candidate_id": candidate.candidate_id,
            "phase": candidate.phase, "decision": decision, "source_onset_at": onset,
            "stop_requested_at": requested, "time_basis": "perf_counter",
            "onset_to_stop_request_ms": None if onset is None else round((requested - onset) * 1000),
            "played_basis": "queue_estimate",
        })

    async def _apply_related(self, related: _Related, interrupt: Callable[[], Awaitable[Any]]) -> None:
        if related.fragments is not self._applying_fragments or not self.owns(related.owner.token) or related.epoch != self._epoch:
            return
        self._applying_fragments = ()
        verdict = related.result.verdict
        reason = related.result.reason
        if related.result.candidate_id != related.candidate.candidate_id or related.result.turn_id != related.candidate.turn_id:
            self._retained.extend(related.fragments)
            return
        selected = related.fragments
        retained = related.fragments
        actions = related.result.segment_actions
        if actions is not None:
            if len(actions) != len(related.fragments) or any(action not in ("floor", "stop", "ignore", "uncertain") for action in actions):
                verdict = "uncertain"
                reason = "invalid_segment_actions"
                selected = ()
            else:
                selected = tuple(fragment for fragment, action in zip(related.fragments, actions) if action == "floor")
                retained = tuple(fragment for fragment, action in zip(related.fragments, actions) if action != "ignore")
                verdict = "directed" if selected else "stop_reply" if "stop" in actions else "uncertain" if "uncertain" in actions else "unrelated"
        self._metrics.record({"kind": "spoken_relation", "request_id": related.owner.id,
                              "revision": related.owner.revision, "candidate_id": related.candidate.candidate_id,
                              "verdict": verdict, "reason": reason,
                              "elapsed_ms": related.result.elapsed_ms,
                              "observed_segments": len(related.fragments),
                              "selected_segments": [index for index, fragment in enumerate(related.fragments) if fragment in selected] if verdict == "directed" else []})
        newer = self._waiting or (related.heard.candidate is not None and self._latest_candidate > related.heard.candidate)
        if newer:
            if verdict != "unrelated":
                self._retained.extend(retained)
            return
        if verdict == "stop_reply":
            stopped = tuple(fragment.heard for index, fragment in enumerate(related.fragments)
                            if actions is None or actions[index] == "stop")
            self._record_stop_request(related.candidate, stopped, "stop")
            played = self._speech.played().strip()
            self._after_voice_stop(related.owner, played, related.heard)
            await interrupt()
            return
        if verdict not in ("directed", "amend", "new_request"):
            if verdict != "unrelated":
                self._retained.extend(retained)
            return
        try:
            transcript = " ".join(fragment.heard.text for fragment in selected).strip()
            updated = related.owner.amended(transcript, related.heard.at, related.heard.lang,
                                            segments=tuple(fragment.heard.text for fragment in selected))
        except ValueError as error:
            self._notice(str(error))
            return
        self._follow_up = None
        self._owned = updated
        self._applying_fragments = ()
        self._reply_started = None
        self._reply_finished = None
        if related.heard.candidate is not None:
            self._accepted_candidate = related.heard.candidate
        played = self._speech.played().strip()
        before = self._voice.state
        self._voice = replace(self._voice, state="thinking", awaiting=updated.arrived, deadline=None,
                              in_turn=False, voiced_at=None, barge=None, cut_off=False)
        if before != "thinking":
            self._transition(before, "thinking", related.heard)
        self._sync_capture()
        line = Line(updated.text, updated.arrived, updated.lang, typed=False, interrupted=played or None,
                    request_id=updated.id, revision=updated.revision, segments=updated.segments, relation_hint=verdict)
        self._admitted(line)
        self._record_stop_request(related.candidate, tuple(fragment.heard for fragment in selected), "floor")
        await interrupt()
        if self.owns(updated.token):
            self._lines.put_nowait(line)
        self._start_assessment()

    def _step(self, event: Event) -> list[Action]:
        before_voice = self._voice
        before, woke_at = before_voice.state, before_voice.woke_at
        if isinstance(event, WakeWord) and event.at < self._typed_at:
            return []
        if isinstance(event, WakeWord) and event.idle_candidate is not None:
            if event.owner != self._epoch or before != "idle":
                return []
        if isinstance(event, (ReplyAudio, ReplyDone)) and event.owner is not None and not self.owns(event.owner):
            return []
        self._voice, actions = step(before_voice, event, self._timing)
        if isinstance(event, SpeechStarted) and before == "follow_up" and self._follow_up is not None:
            self._voice = replace(before_voice, in_turn=True)
        delivered_at = event.delivered_at if isinstance(event, WakeWord) and event.delivered_at is not None else event.at
        if isinstance(event, WakeWord) and not before_voice.in_turn and delivered_at >= before_voice.chime_until:
            self._wake_candidate = event.idle_candidate
            if not self._retire():
                self._voice = before_voice
                self._sync_capture()
                return []
        if any(isinstance(action, SendTurn) for action in actions):
            heard = next(action for action in actions if isinstance(action, SendTurn))
            self._request_sequence += 1
            self._owned = SpokenTurn(f"spoken-{self._request_namespace}-{self._request_sequence}", 0, heard.text, heard.at, heard.lang)
            self._reply_started = None
            self._reply_finished = None
            if isinstance(event, Heard) and event.candidate is not None:
                self._accepted_candidate = event.candidate
        if self._voice.state == "follow_up" and isinstance(event, ReplyDone) and self._voice.deadline is not None:
            if before == "announce" and self._follow_up is not None and self._follow_up.phase == "announce":
                self._follow_up.expires = self._voice.deadline
                self._follow_up.completed_at = event.at
                self._follow_up.played_text = self._speech.played().strip()
            else:
                self._follow_up = FollowUpWindow(
                    f"follow-up-{self._request_namespace}-{event.at}", self._epoch, event.at, self._voice.deadline,
                    self._owned.semantic_text if self._owned is not None and before != "announce" else "",
                    self._speech.played().strip(), self._owned if before != "announce" else None,
                )
        if self._voice.state == "idle" and before_voice.state != "idle":
            preserve = isinstance(event, Tick) and self._follow_up is not None
            if preserve and self._follow_up is not None and not self._follow_up.active and not self._follow_up.inflight:
                self._follow_up.retained = ()
            self._retire(preserve_follow_up=preserve)
        self._sync_capture()
        if isinstance(event, CaptureAborted) and (before_voice.in_turn or event.wake_handoff) and before in CAPTURING:
            if event.wake_handoff:
                reason = "Audio was dropped while waking." if event.reason == "overflow" else "That wake request could not be retained."
            else:
                reason = {
                    "retention_limit": "That request exceeded the retained audio limit.",
                    "overflow": "Audio was dropped while listening fell behind.",
                }.get(event.reason, "That request could not be transcribed.")
            self._notice(f"{reason} Say 'Hey Jarvis' and try again.")
        after = self._voice.state
        if after != before:
            self._transition(before, after, event)
        if self._voice.woke_at != woke_at:
            self._woke()
        return actions

    async def _next_event(self) -> Event | _Related | _FollowUpRelated:
        """The next event, or a Tick once the machine's deadline has come."""
        deadline = self._voice.deadline
        if deadline is None:
            return await self._events.get()
        getting = asyncio.create_task(self._events.get())
        sleeping = asyncio.create_task(self._clock.sleep(max(0.0, deadline - self._clock.now())))
        try:
            await asyncio.wait([getting, sleeping], return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeping.cancel()
            if not getting.done():
                getting.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await getting
        if getting.done() and not getting.cancelled():
            return getting.result()
        return Tick(self._clock.now())

    def _transition(self, before: VoiceState, after: VoiceState, event: Event) -> None:
        self._settle_idle()
        try:
            self._metrics.record({
                "kind": "voice", "from": before, "to": after, "on": type(event).__name__,
                "event_at": event.at, "processed_at": self._clock.now(), "time_basis": "perf_counter",
                "request_at": getattr(event, "answers", self._voice.awaiting),
            })
        except Exception as e:
            self._notice(f"Could not write metrics: {e}")
        if self._debug:
            self._notice(f"voice: {before} -> {after} ({type(event).__name__})")

    def _debug_notice(self, text: str) -> None:
        if self._debug:
            self._notice(text)

    async def _act(self, action: Action, interrupt: Callable[[], Awaitable[Any]]) -> None:
        match action:
            case Chime():
                self._speech.say("chime", "en")
            case SendTurn(text, lang, at, interrupted):
                heard = self._heard_before_cut if interrupted else None
                self._lines.put_nowait(Line(text, at, lang, typed=False, interrupted=heard,
                                           request_id=self._owned.id if self._owned else None,
                                           revision=self._owned.revision if self._owned else 0,
                                           segments=self._owned.segments if self._owned else (text,)))
            case Pause():
                self._player.pause()
                self._debug_notice("voice: paused for a Barge-in")
            case Resume():
                self._player.resume()
                self._debug_notice("voice: resumed after a Backchannel")
            case Interrupt():
                if self._voice.cut_off:
                    self._heard_before_cut = self._speech.played()
                # Awaited before the chime that follows, so the stop cannot flush the chime.
                try:
                    await interrupt()
                except Exception as e:
                    self._notice(f"Could not interrupt the reply: {type(e).__name__}: {e}")


async def merge(typed: AsyncIterator[Line], spoken: AsyncIterator[Line]) -> AsyncIterator[Line]:
    """Typed and spoken lines as they come, until the typed input ends."""
    queue: asyncio.Queue[Line | None] = asyncio.Queue()

    async def pump(lines: AsyncIterator[Line], ends: bool) -> None:
        async for line in lines:
            queue.put_nowait(line)
        if ends:
            queue.put_nowait(None)

    tasks = [asyncio.create_task(pump(typed, True)), asyncio.create_task(pump(spoken, False))]
    try:
        while (line := await queue.get()) is not None:
            yield line
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.wait(tasks)
        for lines in (typed, spoken):
            aclose = getattr(lines, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()
