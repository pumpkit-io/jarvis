"""Speech output: the Talker's streamed text becomes audio sentence by sentence, plus nonverbal cues.

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
from jarvis_hub.voice.player import Clip, PlaybackReceipt, Player, Samples
from jarvis_hub.voice.splitter import SentenceSplitter
from jarvis_hub.voice.tts import Synthesizer

ClipName = Literal["chime", "ready", "closing"]

# Said to the engine at load and never played.
WARM_UP: dict[Lang, str] = {"en": "Good evening, sir.", "it": "Buonasera, signore."}

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


def _notes(sample_rate: int, notes: tuple[float, ...], seconds: float, gain: float) -> Samples:
    note = int(sample_rate * seconds)
    t = np.arange(note) / sample_rate
    envelope = np.sin(np.pi * np.arange(note) / note) ** 2
    return np.concatenate([gain * envelope * np.sin(2 * np.pi * hz * t) for hz in notes]).astype(np.float32)


def chime(sample_rate: int) -> Samples:
    """Two short rising notes."""
    return _notes(sample_rate, (880.0, 1318.5), 0.09, 0.25)


def closing(sample_rate: int) -> Samples:
    """The chime turned around, falling and a little slower and softer: Jarvis has stopped listening."""
    return _notes(sample_rate, (1318.5, 880.0), 0.12, 0.18)


@dataclass
class _Reply:
    turn: int
    lang: Lang
    began: float
    heard_at: float | None
    owner: tuple[str, int] | None = None
    player: Player | None = None
    allowed: Callable[[], bool] | None = None
    splitter: SentenceSplitter = field(default_factory=SentenceSplitter)
    chunks: int = 0
    first_chunk: float | None = None
    first_chunk_audio: float | None = None
    first_audio: float | None = None
    gaps: int = 0
    audio_s: float = 0.0
    generation_done: bool = False
    stopped: bool = False
    closed: bool = False
    failed: bool = False
    played: bool = False
    receipts: list[PlaybackReceipt] = field(default_factory=list[PlaybackReceipt])
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
    cue_generation: int | None = None


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
        self._players: list[Player] = [player]
        self._retired_players: list[Player] = []
        self._last_player = player
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
        self._local_cue_generation = 0
        self._worker: asyncio.Task[None] | None = None
        self._loading: asyncio.Future[None] | None = None

    def start(self) -> None:
        """Load the voice in the background; speech and nonverbal cues wait until it is ready."""
        self._loading = asyncio.get_running_loop().run_in_executor(self._thread, self._load)
        self._worker = asyncio.create_task(self._work(self._loading))

    def say(self, name: ClipName, lang: Lang, *, in_reply: bool = False) -> None:
        """Queue a nonverbal cue; `in_reply` associates its playback with the reply `begin` opened."""
        reply = self._reply if in_reply else None
        if in_reply and reply is None:
            return
        generation = None
        if reply is None:
            self._retired_players = [retired for retired in self._retired_players if retired is not self._player]
            generation = self._local_cue_generation
        self._queue.put_nowait(_Cached(name, lang, reply, generation))

    def begin(self, lang: Lang, *, turn: int, heard_at: float | None, owner: tuple[str, int] | None = None,
              destination: Player | None = None, allowed: Callable[[], bool] | None = None) -> Callable[[], bool]:
        """Bind a reply's destination and return a completion check unaffected by later replies.
        `heard_at` keeps the User's speech-end timestamp; availability is rechecked before synthesis and delivery."""
        player = destination if destination is not None else self._player
        if not any(p is player for p in self._players):
            self._players.append(player)
        self._retired_players = [retired for retired in self._retired_players if retired is not player]
        self._last_player = player
        self._reply = _Reply(turn, lang, self._clock.now(), heard_at, owner=owner, player=player, allowed=allowed)
        self._prune_replies()
        self._replies[turn] = self._reply
        reply = self._reply
        return lambda: self._reply_played(reply)

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
        reply.closed = True
        self._queue.put_nowait(_End(reply))
        self._reply = None

    def stop(self) -> None:
        """Drop the reply and everything queued or playing."""
        self._prune_replies()
        for reply in self._replies.values():
            reply.stopped = True
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
        for player in self._players:
            try:
                player.stop()
            except Exception as e:
                self._notice(f"Could not stop speech: {type(e).__name__}: {e}")

    def retire(self, destination: Player | None) -> None:
        """Stop this output and its playback waits until a new reply or local cue reactivates it."""
        player = destination if destination is not None else self._player
        if player is self._player:
            self._local_cue_generation += 1
        self._prune_replies()
        for reply in self._replies.values():
            if reply.player is player:
                reply.stopped = True
        if self._reply is not None and self._reply.stopped:
            self._reply = None
        if not any(retired is player for retired in self._retired_players):
            self._retired_players.append(player)
            try:
                player.stop()
            except Exception as e:
                self._notice(f"Could not stop speech: {type(e).__name__}: {e}")
        self._prune_replies()

    def played(self) -> str:
        return self._last_player.played()

    async def idle(self, *, stall_timeout: float | None = STALLED) -> None:
        """Wait for voice loading and playback; None disables the stalled-player timeout."""
        if self._loading is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(self._loading)
        await self._queue.join()
        def pending() -> float:
            return sum(p.pending() for p in self._players if not any(retired is p for retired in self._retired_players))

        left, since = pending(), self._clock.now()
        while left > 0:
            await self._clock.sleep(0.05)
            if (now_left := pending()) < left:
                left, since = now_left, self._clock.now()
            elif stall_timeout is not None and self._clock.now() - since >= stall_timeout:
                return

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        self._thread.shutdown(wait=False)
        for player in self._players:
            player.close()

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
        # The first synthesis per voice is seconds slower than the rest, so it is paid here, not by the first reply.
        for lang, phrase in WARM_UP.items():
            self._tts.synthesize(phrase, voice_for(lang))
        tone = chime(self._tts.sample_rate)
        for lang in ("en", "it"):
            self._cache["chime", lang] = Clip("", "chime", tone, self._tts.sample_rate)
            self._cache["ready", lang] = Clip("", "ready", tone, self._tts.sample_rate)
            self._cache["closing", lang] = Clip("", "closing", closing(self._tts.sample_rate), self._tts.sample_rate)

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
                if isinstance(item, _Say):
                    item.reply.failed = True
                self._notice(f"Could not speak a sentence: {type(e).__name__}: {e}")
            finally:
                if isinstance(item, _End):
                    item.reply.generation_done = True
                    self._prune_replies()
                self._queue.task_done()

    def _reply_played(self, reply: _Reply) -> bool:
        if not reply.played and reply.closed and not reply.stopped and not reply.failed and reply.chunks > 0:
            reply.played = len(reply.receipts) == reply.chunks and all(receipt.completed for receipt in reply.receipts)
        return reply.played

    def _prune_replies(self) -> None:
        for reply in self._replies.values():
            self._reply_played(reply)
        self._replies = {
            turn: reply for turn, reply in self._replies.items()
            if not reply.stopped and (not reply.generation_done or (
                reply.audio_s > 0 and (reply.player or self._player).pending() > reply.trailing_audio_s + 1e-9
            ))
        }

    async def _handle(self, item: _Say | _Cached | _End) -> None:
        match item:
            case _Say(reply, text):
                if reply.stopped or (reply.allowed is not None and not reply.allowed()):
                    return
                generation = self._generation
                voice = voice_for(reply.lang)
                samples = await asyncio.get_running_loop().run_in_executor(
                    self._thread, self._tts.synthesize, text, voice
                )
                if generation == self._generation and not reply.stopped and (reply.allowed is None or reply.allowed()):
                    self._play(Clip(text, voice, samples, self._tts.sample_rate), reply, sentence=True)
            case _Cached(name, lang, reply, cue_generation):
                if cue_generation is not None and cue_generation != self._local_cue_generation:
                    return
                if reply is not None and reply.stopped:
                    return
                starts = self._play(self._cache[name, lang], reply, sentence=False)
                if name == "chime" and self._on_chime is not None:
                    self._on_chime(starts)
            case _End(reply):
                self._record_reply(reply)

    def _play(self, clip: Clip, reply: _Reply | None, *, sentence: bool) -> float:
        """Plays `clip` after what is queued; returns when it starts."""
        player = reply.player if reply is not None and reply.player is not None else self._player
        if any(retired is player for retired in self._retired_players) or (
            reply is not None and (reply.stopped or (reply.allowed is not None and not reply.allowed()))
        ):
            return self._clock.now()
        self._prune_replies()
        for queued in self._replies.values():
            if queued.player is player:
                queued.trailing_audio_s += clip.seconds
        if reply is not None:
            reply.trailing_audio_s = 0.0
        starts = self._clock.now() + player.pending()
        starts_reply = reply is not None and reply.first_audio is None
        if reply is not None:
            if reply.first_audio is None:
                reply.first_audio = starts
            if sentence and reply.first_chunk_audio is None:
                reply.first_chunk_audio = starts
            elif sentence and player.pending() == 0:
                reply.gaps += 1
            reply.audio_s += clip.seconds
        receipt = player.play(clip, starts_reply=starts_reply)
        if reply is not None and sentence and clip.seconds > 0:
            reply.receipts.append(receipt)
        if starts_reply and reply is not None and player is self._player and self._on_reply_audio is not None:
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
