"""Voice mode: runs the voice state machine on the audio input's events and the replies' playback.

The User's turns come out as `Line`s for the Hub; the Hub's spoken replies come back through `speech`, so the
machine knows when a reply starts and ends playing.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from jarvis_hub.hub import Line
from jarvis_hub.voice.language import Lang
from jarvis_hub.voice.player import Player
from jarvis_hub.voice.speech import Metrics, Speech, SpeechClock
from jarvis_hub.voice.state import (
    CAPTURING,
    Action,
    AnnouncementBegins,
    Chime,
    ChimeStarted,
    EndConversation,
    Event,
    InputEvent,
    Interrupt,
    ReplyAudio,
    ReplyDone,
    Say,
    SendTurn,
    Tick,
    Timing,
    Voice,
    VoiceState,
    step,
)
from jarvis_hub.voice.tts import Synthesizer


class AudioInput(Protocol):
    """What the input pipeline reports (`jarvis_hub.voice.input.Capture`), or a test's script."""

    def events(self) -> AsyncIterator[InputEvent]: ...

    def capture(self, on: bool) -> None:
        """Whether speech becomes turns. The Wake word is listened for either way."""
        ...


class _ObservedSpeech:
    """The Hub's speech, telling the machine when each reply has finished playing.

    The answer to a turn heard before the latest Wake word is dropped: the User moved on, and it stays on screen.
    """

    def __init__(
        self,
        speech: Speech,
        done: Callable[[float | None], Awaitable[None]],
        moved_on: Callable[[float | None], bool],
    ) -> None:
        self._speech = speech
        self._done = done
        self._moved_on = moved_on
        self._replies = 0
        # Replies up to this one were stopped: their playback ending is not a reply done.
        self._stopped = 0
        self._heard_at: float | None = None
        self._dropping = False
        self._waits: set[asyncio.Task[None]] = set()

    def begin(self, lang: Lang, *, turn: int, heard_at: float | None) -> None:
        self._dropping = self._moved_on(heard_at)
        if self._dropping:
            return
        self._replies += 1
        self._heard_at = heard_at
        self._speech.begin(lang, turn=turn, heard_at=heard_at)

    def feed(self, text: str) -> None:
        if not self._dropping:
            self._speech.feed(text)

    def block_end(self) -> None:
        if not self._dropping:
            self._speech.block_end()

    def end(self) -> None:
        if self._dropping:
            self._dropping = False
            return
        self._speech.end()
        wait = asyncio.create_task(self._played(self._replies, self._heard_at))
        self._waits.add(wait)
        wait.add_done_callback(self._waits.discard)

    def stop(self) -> None:
        self._dropping = True
        self._stopped = self._replies
        self._speech.stop()

    async def _played(self, reply: int, heard_at: float | None) -> None:
        await self._speech.idle()
        # A reply begun since has the floor; its own end reports.
        if reply == self._replies and reply > self._stopped:
            await self._done(heard_at)


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
    ) -> None:
        self._audio = audio
        self._clock = clock
        self._timing = timing
        self._metrics = metrics
        self._notice = notice
        self._debug = debug
        self._voice = Voice()
        self._idle = asyncio.Event()
        self._idle.set()
        self._woke: Callable[[], None] = lambda: None
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._lines: asyncio.Queue[Line] = asyncio.Queue()
        self._speech = Speech(
            tts, player, clock, metrics=metrics, notice=notice, on_reply_audio=self._reply_audio, on_chime=self._chime_started
        )
        self.speech = _ObservedSpeech(self._speech, self._reply_done, self._moved_on)

    @property
    def state(self) -> VoiceState:
        return self._voice.state

    def start(self) -> None:
        """Loads the voice in the background, then says the startup line."""
        self._speech.start()
        self._speech.say("startup", "en")

    def end_conversation(self) -> None:
        self._events.put_nowait(EndConversation(self._clock.now()))

    async def lines(
        self, interrupt: Callable[[], Awaitable[Any]], woke: Callable[[], None] = lambda: None
    ) -> AsyncIterator[Line]:
        """The User's spoken turns, for as long as this is iterated. `woke` hears of each Wake word that opens
        listening, before the turn that follows it."""
        self._woke = woke
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
        """Returns once the machine is idle and nothing is playing, such as a cached line said on the way to idle."""
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
        await self._speech.close()

    def _reply_audio(self, heard_at: float | None) -> None:
        self._events.put_nowait(ReplyAudio(self._clock.now(), heard_at))

    def _chime_started(self, at: float) -> None:
        self._events.put_nowait(ChimeStarted(at))

    async def _reply_done(self, heard_at: float | None) -> None:
        self._events.put_nowait(ReplyDone(self._clock.now(), heard_at))

    def _moved_on(self, heard_at: float | None) -> bool:
        woke_at = self._voice.woke_at
        return heard_at is not None and woke_at is not None and heard_at < woke_at

    async def _listen(self) -> None:
        async for event in self._audio.events():
            self._events.put_nowait(event)

    async def _drive(self, interrupt: Callable[[], Awaitable[Any]]) -> None:
        while True:
            for action in self._step(await self._next_event()):
                await self._act(action, interrupt)

    def _step(self, event: Event) -> list[Action]:
        before, woke_at = self._voice.state, self._voice.woke_at
        self._voice, actions = step(self._voice, event, self._timing)
        after = self._voice.state
        if after != before:
            self._transition(before, after, event)
        if self._voice.woke_at != woke_at:
            self._woke()
        return actions

    async def _next_event(self) -> Event:
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
        capturing = after in CAPTURING
        if capturing != (before in CAPTURING):
            self._audio.capture(capturing)
        try:
            self._metrics.record({"kind": "voice", "from": before, "to": after, "on": type(event).__name__})
        except Exception as e:
            self._notice(f"Could not write metrics: {e}")
        if self._debug:
            self._notice(f"voice: {before} -> {after} ({type(event).__name__})")

    async def _act(self, action: Action, interrupt: Callable[[], Awaitable[Any]]) -> None:
        match action:
            case Chime():
                self._speech.say("chime", "en")
            case Say(name, lang):
                self._speech.say(name, lang)
            case SendTurn(text, lang, at):
                self._lines.put_nowait(Line(text, at, lang, typed=False))
            case Interrupt():
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
