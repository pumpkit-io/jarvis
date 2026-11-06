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
from jarvis_hub.voice.ownership import MAX_REQUEST_CHARACTERS, SpokenTurn
from jarvis_hub.voice.bargein_log import BargeInLog
from jarvis_hub.voice.language import Lang
from jarvis_hub.voice.player import Player
from jarvis_hub.voice.speech import ClipName, Metrics, Speech, SpeechClock
from jarvis_hub.voice.state import (
    CAPTURING,
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
    barge_in_from,
    step,
)
from jarvis_hub.voice.tts import Synthesizer


class AudioInput(Protocol):
    """What the input pipeline reports (`jarvis_hub.voice.input.Capture`), or a test's script."""

    def events(self) -> AsyncIterator[InputEvent]: ...

    def capture(self, on: bool, *, owner: int | None = None) -> None:
        """Whether speech becomes turns. The Wake word is listened for either way."""
        ...

    def barge_in(self, since: float | None) -> None:
        """Report speech held from `since` on as a Barge-in, with partial transcripts, or nothing with None.
        Each call forgets the Barge-in under way, so the next one needs speech held anew."""
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
        self._voice = Voice()
        self._recognition = recognition
        self._epoch = 0
        self._request_sequence = 0
        self._request_namespace = uuid4().hex
        self._admitted: Callable[[Line], None] = lambda line: None
        self._owned: SpokenTurn | None = None
        self._reply_started: tuple[tuple[str, int], float] | None = None
        self._reply_finished: tuple[tuple[str, int], float] | None = None
        self._closed = False
        self._capture_setting: tuple[bool, int] | None = None
        self._origins: dict[int | None, tuple[str, bool | None]] = {}
        self._waiting: deque[_Fragment] = deque()
        self._retained: deque[_Fragment] = deque()
        self._inflight: tuple[_Fragment, ...] = ()
        self._applying_fragments: tuple[_Fragment, ...] = ()
        self._latest_candidate = -1
        self._accepted_candidate = -1
        self._assessing: asyncio.Task[None] | None = None
        self._applying: set[asyncio.Task[None]] = set()
        self._bargein_log = BargeInLog(metrics.record, notice, timing)
        self._barge_in_from: float | None = None
        # What the User heard of the voice they last cut off, for their next turn.
        self._heard_before_cut = ""
        self._idle = asyncio.Event()
        self._idle.set()
        self._woke: Callable[[], None] = lambda: None
        self._events: asyncio.Queue[Event | _Related] = asyncio.Queue()
        self._lines: asyncio.Queue[Line] = asyncio.Queue()
        self._speech = Speech(
            tts, player, clock, metrics=metrics, notice=notice, on_reply_audio=self._reply_audio, on_chime=self._chime_started, on_ready=self._ready
        )
        self.speech = _ObservedSpeech(self._speech, self._reply_done, self._moved_on)

    @property
    def state(self) -> VoiceState:
        return self._voice.state

    def start(self) -> None:
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
            if self._voice.state == "idle":
                return

    def announce(self) -> bool:
        """Moves an idle machine into an Announcement at once, so a Wake word from now on interrupts it.

        Returns False, changing nothing, when Jarvis is not idle: the news waits for the next `quiet`.
        """
        if self._voice.state != "idle":
            return False
        for action in self._step(AnnouncementBegins(self._clock.now())):
            if isinstance(action, Chime):
                # Queued now, so it plays ahead of the news turn's first sentence.
                self._speech.say("chime", "en")
        return True

    async def close(self) -> None:
        self._closed = True
        self._retire()
        pending = [task for task in [self._assessing, *self._applying] if task is not None]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending)
        if self._recognition is not None:
            await self._recognition.close()
        self._bargein_log.close()
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
            if isinstance(event, (Heard, SpeechStarted, CaptureAborted)):
                if event.owner is not None and event.owner != self._epoch:
                    continue
                if event.candidate is not None:
                    if event.candidate <= self._accepted_candidate:
                        continue
                    self._latest_candidate = max(self._latest_candidate, event.candidate)
            if isinstance(event, CaptureAborted):
                self._waiting.clear()
                self._retained.clear()
                self._inflight = ()
                self._applying_fragments = ()
                self._origins.clear()
                if event.candidate is not None:
                    self._accepted_candidate = max(self._accepted_candidate, event.candidate)
                if self._owned is not None and self._voice.state == "thinking":
                    self._notice("That additional speech was lost. Say 'Hey Jarvis' and try again.")
            if isinstance(event, Heard) and event.limited:
                self._notice("Spoken input reached its duration limit. Repeat or split the request.")
                if self._voice.state in CAPTURING:
                    before = self._voice.state
                    self._voice = Voice()
                    self._retire()
                    self._transition(before, "idle", event)
                continue
            if isinstance(event, SpeechStarted):
                if len(self._origins) >= 64:
                    self._origins.pop(next(iter(self._origins)))
                self._origins[event.candidate] = self._origin(event.started_at if event.started_at is not None else event.at)
            if isinstance(event, Heard) and self._owned is not None:
                origin = self._origins.get(event.candidate, self._origin(event.started_at))
                if origin[0] == "thinking" or self._retained or self._inflight or self._applying_fragments or self._waiting:
                    buffered = [*self._retained, *self._inflight, *self._applying_fragments, *self._waiting]
                    text = " ".join([self._owned.text, *(fragment.heard.text for fragment in buffered), event.text])
                    if len(buffered) >= 4 or len(text) > MAX_REQUEST_CHARACTERS:
                        self._notice("Spoken input reached its retention limit. Repeat or split the request.")
                    else:
                        self._waiting.append(_Fragment(event, *origin))
                        self._start_assessment()
                    continue
            for action in self._step(event):
                await self._act(action, interrupt)

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

    def _retire(self) -> None:
        self._epoch += 1
        self._owned = None
        self._reply_started = None
        self._reply_finished = None
        self._accepted_candidate = -1
        self._waiting.clear()
        self._retained.clear()
        self._inflight = ()
        self._applying_fragments = ()
        self._latest_candidate = -1
        self._origins.clear()
        self._sync_capture()

    def semantic_context(self, owner: tuple[str, int], start: int) -> None:
        if self.owns(owner) and self._owned is not None and 0 <= start < len(self._owned.segments):
            self._owned = replace(self._owned, semantic_start=start)

    def typed_input(self) -> None:
        # STT can hold an initial recording before its queued onset reaches this state machine.
        if self._owned is None and self._voice.state in CAPTURING:
            return
        before = self._voice.state
        self._voice = Voice()
        self._retire()
        if before != "idle":
            self._transition(before, "idle", EndConversation(self._clock.now()))
        self._idle.set()

    def _sync_capture(self) -> None:
        setting = (not self._closed and (self._voice.state in CAPTURING or self._voice.state == "thinking"), self._epoch)
        if setting != self._capture_setting:
            self._capture_setting = setting
            self._audio.capture(setting[0], owner=setting[1])

    def _applied(self, task: asyncio.Task[None]) -> None:
        self._applying.discard(task)
        self._applying_fragments = ()
        self._start_assessment()

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
            self._heard_before_cut = self._speech.played().strip()
            before = self._voice.state
            self._voice = replace(self._voice, state="listening", in_turn=False, awaiting=None,
                                  deadline=self._clock.now() + self._timing.listen_s, barge=None, cut_off=False)
            self._retire()
            self._transition(before, "listening", related.heard)
            self.speech.stop()
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
        await interrupt()
        if self.owns(updated.token):
            self._lines.put_nowait(line)
        self._start_assessment()

    def _step(self, event: Event) -> list[Action]:
        before_voice = self._voice
        before, woke_at = before_voice.state, before_voice.woke_at
        if isinstance(event, (ReplyAudio, ReplyDone)) and event.owner is not None and not self.owns(event.owner):
            return []
        self._voice, actions = step(before_voice, event, self._timing)
        if isinstance(event, WakeWord) and not before_voice.in_turn and event.at >= before_voice.chime_until:
            self._retire()
        if any(isinstance(action, SendTurn) for action in actions):
            heard = next(action for action in actions if isinstance(action, SendTurn))
            self._request_sequence += 1
            self._owned = SpokenTurn(f"spoken-{self._request_namespace}-{self._request_sequence}", 0, heard.text, heard.at, heard.lang)
            self._reply_started = None
            self._reply_finished = None
            if isinstance(event, Heard) and event.candidate is not None:
                self._accepted_candidate = event.candidate
        if self._voice.state == "idle" and before_voice.state != "idle":
            self._retire()
        self._sync_capture()
        if isinstance(event, CaptureAborted) and before_voice.in_turn and before in CAPTURING:
            reason = {
                "retention_limit": "That request exceeded the retained audio limit.",
                "overflow": "Audio was dropped while listening fell behind.",
            }.get(event.reason, "That request could not be transcribed.")
            self._notice(f"{reason} Say 'Hey Jarvis' and try again.")
        self._bargein_log.observe(before_voice, event, self._voice, actions, self._speech.played)
        after = self._voice.state
        if after != before:
            self._transition(before, after, event)
        # After capture: a turn that starts now keeps the speech of the Barge-in that stopped Jarvis.
        if (since := barge_in_from(self._voice, self._timing)) != self._barge_in_from:
            self._barge_in_from = since
            self._audio.barge_in(since)
        if self._voice.woke_at != woke_at:
            self._woke()
        return actions

    async def _next_event(self) -> Event | _Related:
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
        if after == "idle":
            self._idle.set()
        else:
            self._idle.clear()
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
