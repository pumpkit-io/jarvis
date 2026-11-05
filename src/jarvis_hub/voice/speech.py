"""Speech output: the Talker's streamed text becomes audio sentence by sentence, plus the Hub's cached lines.

Synthesis runs on one thread of its own and never waits for playback, so sentence n+1 is synthesised while
sentence n plays and the player starts each clip the moment the one before it ends.
"""

import asyncio
import contextlib
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Executor, Future
from dataclasses import dataclass, field
from typing import Any, Literal, ParamSpec, Protocol, TypeVar

import numpy as np

from jarvis_hub.voice.language import Lang, voice_for
from jarvis_hub.voice.player import Clip, Player, Samples
from jarvis_hub.voice.splitter import SentenceSplitter
from jarvis_hub.voice.tts import Synthesizer

ClipName = Literal["chime", "ready", "very_good"]

LINES: dict[ClipName, dict[Lang, str]] = {
    "very_good": {"en": "Very good, sir.", "it": "Molto bene, signore."},
}

# With queued audio this long unplayed, as from a paused player or a device that stopped pulling, `idle` gives up.
STALLED = 2.0


class SpeechClock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class Metrics(Protocol):
    def record(self, entry: dict[str, Any]) -> None: ...


P = ParamSpec("P")
T = TypeVar("T")


class _DaemonThread(Executor):
    """One thread for every call, a daemon so a model download still running never holds up the process's exit."""

    def __init__(self, name: str) -> None:
        self._jobs: queue.SimpleQueue[Callable[[], None] | None] = queue.SimpleQueue()
        threading.Thread(target=self._run, name=name, daemon=True).start()

    def submit(self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> "Future[T]":
        future: Future[T] = Future()

        def job() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as e:
                future.set_exception(e)

        self._jobs.put(job)
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self._jobs.put(None)

    def _run(self) -> None:
        while (job := self._jobs.get()) is not None:
            job()


def chime(sample_rate: int) -> Samples:
    """Two short rising notes."""
    note = int(sample_rate * 0.09)
    t = np.arange(note) / sample_rate
    envelope = np.sin(np.pi * np.arange(note) / note) ** 2
    tones = [0.25 * envelope * np.sin(2 * np.pi * hz * t) for hz in (880.0, 1318.5)]
    return np.concatenate(tones).astype(np.float32)


@dataclass
class _Reply:
    turn: int
    lang: Lang
    began: float
    heard_at: float | None
    owner: tuple[str, int] | None = None
    splitter: SentenceSplitter = field(default_factory=SentenceSplitter)
    chunks: int = 0
    first_chunk: float | None = None
    first_chunk_audio: float | None = None
    first_audio: float | None = None
    gaps: int = 0
    audio_s: float = 0.0
    generation_done: bool = False
    # Later clips remain in pending() after this reply has drained.
    trailing_audio_s: float = 0.0


@dataclass(frozen=True)
class _Say:
    reply: _Reply
    text: str


@dataclass(frozen=True)
class _Cached:
    name: ClipName
    lang: Lang
    reply: _Reply | None = None


@dataclass(frozen=True)
class _End:
    reply: _Reply


class Speech:
    """`begin` a reply, `feed` it the Talker's text, `block_end` each text block and `end` it; `stop` drops the rest.
    A sentence that fails to synthesise is skipped with a notice; if the voice fails to load, replies are text only."""

    def __init__(
        self,
        tts: Synthesizer,
        player: Player,
        clock: SpeechClock,
        *,
        metrics: Metrics,
        notice: Callable[[str], None],
        on_reply_audio: Callable[[float | None, tuple[str, int] | None], None] | None = None,
        on_chime: Callable[[float], None] | None = None,
        on_ready: Callable[[], None] | None = None,
    ) -> None:
        self._tts = tts
        self._player = player
        self._clock = clock
        self._metrics = metrics
        self._notice = notice
        # Called with the reply's `heard_at` when its first audio starts.
        self._on_reply_audio = on_reply_audio
        # Called with the time the chime starts playing, behind whatever is queued ahead of it.
        self._on_chime = on_chime
        self._on_ready = on_ready
        # One thread: MLX ties its streams to the thread that loaded the model.
        self._thread = _DaemonThread("tts")
        self._queue: asyncio.Queue[_Say | _Cached | _End] = asyncio.Queue()
        self._cache: dict[tuple[ClipName, Lang], Clip] = {}
        self._reply: _Reply | None = None
        self._replies: dict[int, _Reply] = {}
        self._generation = 0
        self._worker: asyncio.Task[None] | None = None
        self._loading: asyncio.Future[None] | None = None

    def start(self) -> None:
        """Load the voice and synthesise the cached lines in the background; what is said meanwhile waits."""
        self._loading = asyncio.get_running_loop().run_in_executor(self._thread, self._load)
        self._worker = asyncio.create_task(self._work(self._loading))

    def say(self, name: ClipName, lang: Lang, *, in_reply: bool = False) -> None:
        """Plays a cached line after what is queued; with `in_reply`, as the start of the reply `begin` opened."""
        self._queue.put_nowait(_Cached(name, lang, self._reply if in_reply else None))

    def begin(self, lang: Lang, *, turn: int, heard_at: float | None, owner: tuple[str, int] | None = None) -> None:
        """`heard_at` retains the User's speech-end timestamp for latency and reply ownership."""
        self._reply = _Reply(turn, lang, self._clock.now(), heard_at, owner=owner)
        self._prune_replies()
        self._replies[turn] = self._reply

    def feed(self, text: str) -> None:
        reply = self._reply
        if reply is None:
            return
        self._say(reply.splitter.feed(text))

    def block_end(self) -> None:
        if self._reply is not None:
            self._say(self._reply.splitter.end_block())

    def end(self) -> None:
        reply = self._reply
        if reply is None:
            return
        self._say(reply.splitter.end_block())
        self._queue.put_nowait(_End(reply))
        self._reply = None

    def stop(self) -> None:
        """Drop the reply and everything queued or playing."""
        self._prune_replies()
        for reply in self._replies.values():
            self._record({
                "kind": "speech_stop", "turn": reply.turn, "request_at": reply.heard_at,
                "request_id": reply.owner[0] if reply.owner is not None else None,
                "revision": reply.owner[1] if reply.owner is not None else None,
                "stopped_at": self._clock.now(), "time_basis": "perf_counter",
                "first_meaningful_audio_ms": None if reply.first_chunk_audio is None else round(
                    (reply.first_chunk_audio - (reply.heard_at if reply.heard_at is not None else reply.began)) * 1000
                ),
                "meaningful_audio_basis": "queue_estimate",
            })
        self._replies.clear()
        self._generation += 1
        self._reply = None
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        self._player.stop()

    def played(self) -> str:
        return self._player.played()

    async def idle(self) -> None:
        """Returns once the voice has loaded, or failed to, and everything said so far has finished playing."""
        if self._loading is not None:
            with contextlib.suppress(Exception):
                await self._loading
        await self._queue.join()
        left, since = self._player.pending(), self._clock.now()
        while left > 0:
            await self._clock.sleep(0.05)
            if (now_left := self._player.pending()) < left:
                left, since = now_left, self._clock.now()
            elif self._clock.now() - since >= STALLED:
                return

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        self._thread.shutdown(wait=False)
        self._player.close()

    def _say(self, chunks: list[str]) -> None:
        reply = self._reply
        assert reply is not None
        for chunk in chunks:
            if reply.first_chunk is None:
                reply.first_chunk = self._clock.now()
            reply.chunks += 1
            self._queue.put_nowait(_Say(reply, chunk))

    def _load(self) -> None:
        self._tts.load()
        for name, lines in LINES.items():
            for lang, text in lines.items():
                voice = voice_for(lang)
                self._cache[name, lang] = Clip(text, voice, self._tts.synthesize(text, voice), self._tts.sample_rate)
        tone = chime(self._tts.sample_rate)
        for lang in ("en", "it"):
            self._cache["chime", lang] = Clip("", "chime", tone, self._tts.sample_rate)
            self._cache["ready", lang] = Clip("", "ready", tone, self._tts.sample_rate)

    async def _work(self, loading: "asyncio.Future[None]") -> None:
        began = self._clock.now()
        try:
            await loading
            ready = True
            self._record({"kind": "voice_loaded", "load_ms": round((self._clock.now() - began) * 1000)})
            if self._on_ready is not None:
                self._on_ready()
        except Exception as e:
            self._notice(f"Speech is off: the voice could not load: {type(e).__name__}: {e}")
            ready = False
        while True:
            item = await self._queue.get()
            try:
                if ready:
                    await self._handle(item)
            except Exception as e:
                self._notice(f"Could not speak a sentence: {type(e).__name__}: {e}")
            finally:
                if isinstance(item, _End):
                    item.reply.generation_done = True
                    self._prune_replies()
                self._queue.task_done()

    def _prune_replies(self) -> None:
        pending = self._player.pending()
        self._replies = {
            turn: reply for turn, reply in self._replies.items()
            if not reply.generation_done or (reply.audio_s > 0 and pending > reply.trailing_audio_s + 1e-9)
        }

    async def _handle(self, item: _Say | _Cached | _End) -> None:
        match item:
            case _Say(reply, text):
                generation = self._generation
                voice = voice_for(reply.lang)
                samples = await asyncio.get_running_loop().run_in_executor(
                    self._thread, self._tts.synthesize, text, voice
                )
                if generation == self._generation:
                    self._play(Clip(text, voice, samples, self._tts.sample_rate), reply, sentence=True)
            case _Cached(name, lang, reply):
                starts = self._play(self._cache[name, lang], reply, sentence=False)
                if name == "chime" and self._on_chime is not None:
                    self._on_chime(starts)
            case _End(reply):
                self._record_reply(reply)

    def _play(self, clip: Clip, reply: _Reply | None, *, sentence: bool) -> float:
        """Plays `clip` after what is queued; returns when it starts."""
        self._prune_replies()
        for queued in self._replies.values():
            queued.trailing_audio_s += clip.seconds
        if reply is not None:
            reply.trailing_audio_s = 0.0
        starts = self._clock.now() + self._player.pending()
        starts_reply = reply is not None and reply.first_audio is None
        if reply is not None:
            if reply.first_audio is None:
                reply.first_audio = starts
            if sentence and reply.first_chunk_audio is None:
                reply.first_chunk_audio = starts
            elif sentence and self._player.pending() == 0:
                reply.gaps += 1
            reply.audio_s += clip.seconds
        self._player.play(clip, starts_reply=starts_reply)
        if starts_reply and reply is not None and self._on_reply_audio is not None:
            self._on_reply_audio(reply.heard_at, reply.owner)
        return starts

    def _record_reply(self, reply: _Reply) -> None:
        start = reply.heard_at if reply.heard_at is not None else reply.began

        def ms(t: float | None) -> int | None:
            return None if t is None else round((t - start) * 1000)

        chunk_to_audio = None
        if reply.first_chunk is not None and reply.first_chunk_audio is not None:
            chunk_to_audio = round((reply.first_chunk_audio - reply.first_chunk) * 1000)
        entry = {
            "kind": "speech",
            "turn": reply.turn,
            "lang": reply.lang,
            "request_at": reply.heard_at,
            "request_id": reply.owner[0] if reply.owner is not None else None,
            "revision": reply.owner[1] if reply.owner is not None else None,
            "started_at": reply.began,
            "time_basis": "perf_counter",
            "first_meaningful_audio_ms": ms(reply.first_chunk_audio),
            "meaningful_audio_basis": "queue_estimate",
            "voice": voice_for(reply.lang),
            "chunks": reply.chunks,
            "first_chunk_ms": ms(reply.first_chunk),
            "first_audio_ms": ms(reply.first_audio),
            "chunk_to_audio_ms": chunk_to_audio,
            "gaps": reply.gaps,
            "audio_s": round(reply.audio_s, 2),
        }
        self._record(entry)

    def _record(self, entry: dict[str, Any]) -> None:
        try:
            self._metrics.record(entry)
        except Exception as e:
            self._notice(f"Could not write metrics: {e}")
