"""Speech output: the Talker's streamed text becomes audio sentence by sentence, plus nonverbal cues.

Synthesis runs on one thread of its own and never waits for playback, so sentence n+1 is synthesised while
sentence n plays and the player starts each clip the moment the one before it ends. An engine may stream a sentence
in pieces, each played as a clip of its own as soon as it is ready.
"""

import asyncio
import contextlib
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Executor, Future
from dataclasses import dataclass, field
from typing import Any, Literal, ParamSpec, Protocol, TypeVar
from uuid import uuid4

import numpy as np

from jarvis_hub.voice.language import Lang, detect_language
from jarvis_hub.voice.player import Clip, PlaybackReceipt, Player, RemotePlayer, Samples
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
            job = None


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
    # Starts as the User's language and follows the text spoken: the voice of the next chunk of unclear language.
    lang: Lang
    began: float
    heard_at: float | None
    owner: tuple[str, int] | None = None
    floor: bool = False
    player: Player | None = None
    # The Client the reply speaks on, for its metrics rows.
    client: str | None = None
    reply_id: str | None = None
    speech_id: str = field(default_factory=lambda: uuid4().hex)
    stream_started: bool = False
    allowed: Callable[[], bool] | None = None
    can_start: Callable[[], bool] | None = None
    splitter: SentenceSplitter = field(default_factory=SentenceSplitter)
    chunks: int = 0
    # Chunks whose every piece reached the player.
    spoken: int = 0
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
    lang: Lang


@dataclass(frozen=True)
class _Cached:
    name: ClipName
    lang: Lang
    reply: _Reply | None = None
    # A cue outside a reply plays only while its destination's cues have not been dropped since it was queued.
    epoch: object | None = None
    # Where a cue outside a reply plays; None for the Mac.
    player: Player | None = None


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
        self._last_player: Player | None = player
        self._last_played = ""
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
        self._queued_by_player: dict[Player, int] = {}
        self._queue_changed = asyncio.Event()
        self._cache: dict[tuple[ClipName, Lang], Clip] = {}
        self._reply: _Reply | None = None
        self._replies: dict[int, _Reply] = {}
        self._replay_turn = 0
        self._replays: dict[RemotePlayer, _Reply] = {}
        self._generation = 0
        self._cue_epochs: dict[Player, object] = {}
        self._worker: asyncio.Task[None] | None = None
        self._loading: asyncio.Future[None] | None = None

    def start(self, *, lazy: bool = False) -> None:
        """Load the voice in the background, or defer loading until a reply begins with `lazy`."""
        if lazy or self._worker is not None:
            return
        self._loading = asyncio.get_running_loop().run_in_executor(self._thread, self._load)
        self._worker = asyncio.create_task(self._work(self._loading))

    def say(self, name: ClipName, lang: Lang, *, in_reply: bool = False, destination: Player | None = None) -> None:
        """Queue a nonverbal cue; `in_reply` associates its playback with the reply `begin` opened, and
        `destination` plays one outside a reply somewhere other than the Mac."""
        reply = self._reply if in_reply else None
        if in_reply and reply is None:
            return
        if reply is not None:
            self._enqueue(_Cached(name, lang, reply))
            return
        epoch = self._cue_epochs.setdefault(destination if destination is not None else self._player, object())
        self._enqueue(_Cached(name, lang, None, epoch, destination))

    def drop_cues(self, destination: Player | None) -> None:
        """Cues queued outside a reply for `destination`, or the Mac, are skipped; later ones play."""
        self._cue_epochs.pop(destination if destination is not None else self._player, None)

    def begin(self, lang: Lang, *, turn: int, heard_at: float | None, owner: tuple[str, int] | None = None,
              destination: Player | None = None, allowed: Callable[[], bool] | None = None,
              can_start: Callable[[], bool] | None = None, reply_id: str | None = None,
              client: str | None = None, floor: bool = False) -> Callable[[], bool]:
        """Bind a reply's destination and return a completion check unaffected by later replies.
        `can_start` gates only the first audio; `allowed` continues to guard an active reply."""
        self.start()
        player = destination if destination is not None else self._player
        if not any(p is player for p in self._players):
            self._players.append(player)
        self._last_player = player
        self._reply = _Reply(turn, lang, self._clock.now(), heard_at, owner=owner, player=player, client=client,
                             reply_id=reply_id, allowed=allowed, can_start=can_start, floor=floor)
        self._prune_replies()
        self._replies[turn] = self._reply
        reply = self._reply
        return lambda: self._reply_played(reply)

    def replay(self, text: str, lang: Lang, *, destination: RemotePlayer, reply_id: str,
               allowed: Callable[[], bool]) -> str:
        """Queue retained text without replacing the Talker's active input stream."""
        self.start()
        if previous := self._replays.get(destination):
            previous.stopped = True
            if destination.owns(previous.speech_id):
                destination.stop()
        self._replay_turn -= 1
        reply = _Reply(self._replay_turn, lang, self._clock.now(), None, player=destination,
                       reply_id=reply_id, allowed=allowed)
        self._replays[destination] = reply
        if not any(p is destination for p in self._players):
            self._players.append(destination)
        self._replies[reply.turn] = reply
        self._say(reply.splitter.feed(text) + reply.splitter.end_block(), reply)
        reply.closed = True
        self._enqueue(_End(reply))
        return reply.speech_id

    def stop_replay(self, destination: Player) -> bool:
        reply = self._replays.get(destination) if isinstance(destination, RemotePlayer) else None
        if reply is None or not isinstance(destination, RemotePlayer) or not destination.owns(reply.speech_id):
            return False
        reply.stopped = True
        destination.stop()
        return True

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
        self._enqueue(_End(reply))
        self._reply = None

    def stop(self) -> None:
        """Drop the reply and everything queued or playing."""
        self._prune_replies()
        for reply in self._replies.values():
            reply.stopped = True
            self._record({
                "kind": "speech_stop", "turn": reply.turn, "request_at": reply.heard_at,
                "request_id": reply.owner[0] if reply.owner is not None else None,
                "revision": reply.owner[1] if reply.owner is not None else None, "client": reply.client,
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
            self._completed(self._queue.get_nowait())
        for player in self._players:
            try:
                player.stop()
            except Exception as e:
                self._notice(f"Could not stop speech: {type(e).__name__}: {e}")

    def retire(self, destination: Player | None) -> None:
        """Stop this output and its playback waits until a new reply or local cue reactivates it."""
        player = destination if destination is not None else self._player
        if player is self._player:
            self.drop_cues(None)
        self._prune_replies()
        for reply in self._replies.values():
            if reply.player is player:
                reply.stopped = True
        if self._reply is not None and self._reply.stopped:
            self._reply = None
        if self._last_player is player:
            self._last_played = player.played()
            self._last_player = None
        self._players = [p for p in self._players if p is not player or p is self._player]
        if isinstance(player, RemotePlayer):
            self._replays.pop(player, None)
        try:
            player.stop()
        except Exception as e:
            self._notice(f"Could not stop speech: {type(e).__name__}: {e}")
        self._prune_replies()

    def played(self) -> str:
        return self._last_player.played() if self._last_player is not None else self._last_played

    async def idle(self, *, stall_timeout: float | None = STALLED, local_only: bool = False,
                   destination: Player | None = None) -> None:
        """Wait for queued synthesis and playback, optionally for one destination.
        None disables the stalled-player timeout."""
        if destination is None:
            if self._loading is not None:
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._loading)
            await self._queue.join()
        else:
            while self._queued_by_player.get(destination, 0):
                self._queue_changed.clear()
                await self._queue_changed.wait()
        def pending() -> float:
            return sum(p.pending() for p in self._players
                       if (not local_only or p is self._player) and (destination is None or p is destination))

        left, since = pending(), self._clock.now()
        while left > 0:
            await self._clock.sleep(0.05)
            if (now_left := pending()) < left:
                left, since = now_left, self._clock.now()
            elif stall_timeout is not None and self._clock.now() - since >= stall_timeout:
                return
        self._prune_replies()

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        while not self._queue.empty():
            self._completed(self._queue.get_nowait())
        self._thread.shutdown(wait=False)
        for player in self._players:
            player.close()

    def _destination(self, item: _Say | _Cached | _End) -> Player:
        reply = item.reply
        if reply is not None and reply.player is not None:
            return reply.player
        return item.player if isinstance(item, _Cached) and item.player is not None else self._player

    def _enqueue(self, item: _Say | _Cached | _End) -> None:
        player = self._destination(item)
        self._queued_by_player[player] = self._queued_by_player.get(player, 0) + 1
        self._queue.put_nowait(item)

    def _completed(self, item: _Say | _Cached | _End) -> None:
        player = self._destination(item)
        remaining = self._queued_by_player[player] - 1
        if remaining:
            self._queued_by_player[player] = remaining
        else:
            del self._queued_by_player[player]
        self._queue_changed.set()
        self._queue.task_done()

    def _say(self, chunks: list[str], reply: _Reply | None = None) -> None:
        reply = reply or self._reply
        assert reply is not None
        for chunk in chunks:
            if reply.first_chunk is None:
                reply.first_chunk = self._clock.now()
            reply.chunks += 1
            # Each chunk is spoken in its own language; one too short to tell keeps the one before it.
            reply.lang = detect_language(chunk, reply.lang)
            self._enqueue(_Say(reply, chunk, reply.lang))

    def _load(self) -> None:
        self._tts.load()
        # The first synthesis per voice is seconds slower than the rest, so it is paid here, not by the first reply.
        for lang, phrase in WARM_UP.items():
            for _ in self._tts.stream(phrase, self._tts.voice_for(lang)):
                pass
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
                self._completed(item)
                del item

    def _reply_played(self, reply: _Reply) -> bool:
        if not reply.played and reply.closed and not reply.stopped and not reply.failed and reply.chunks > 0:
            reply.played = reply.spoken == reply.chunks and all(receipt.completed for receipt in reply.receipts)
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

        self._replays = {player: reply for player, reply in self._replays.items()
                         if reply.turn in self._replies}

    @staticmethod
    def _reply_allowed(reply: _Reply) -> bool:
        if reply.stopped or (reply.allowed is not None and not reply.allowed()):
            return False
        if reply.first_audio is None and reply.can_start is not None and not reply.can_start():
            # Dropping the opening must never allow a later sentence to start this reply.
            reply.stopped = True
            return False
        return True

    async def _handle(self, item: _Say | _Cached | _End) -> None:
        match item:
            case _Say(reply, text, lang):
                if not self._reply_allowed(reply):
                    return
                generation = self._generation
                voice = self._tts.voice_for(lang)
                run = asyncio.get_running_loop().run_in_executor
                pieces = await run(self._thread, self._tts.stream, text, voice)
                try:
                    while (piece := await run(self._thread, next, pieces, None)) is not None:
                        if generation != self._generation or not self._reply_allowed(reply):
                            return
                        words, samples = piece
                        self._play(Clip(words, voice, samples, self._tts.sample_rate), reply, sentence=True)
                    reply.spoken += 1
                finally:
                    # Closed on the synthesis thread: the engine's cleanup may touch MLX.
                    await run(self._thread, pieces.close)
            case _Cached(name, lang, reply, epoch, player):
                if epoch is not None and epoch is not self._cue_epochs.get(player if player is not None else self._player):
                    return
                if reply is not None and reply.stopped:
                    return
                starts = self._play(self._cache[name, lang], reply, sentence=False, player=player)
                if name == "chime" and self._on_chime is not None:
                    self._on_chime(starts)
            case _End(reply):
                if not reply.stopped and reply.first_audio is not None and isinstance(reply.player, RemotePlayer) and reply.player.owns(reply.speech_id):
                    reply.player.end_reply()
                self._record_reply(reply)

    def _play(self, clip: Clip, reply: _Reply | None, *, sentence: bool, player: Player | None = None) -> float:
        """Plays `clip` after what is queued; returns when it starts. A reply's clip plays on the reply's player,
        any other on `player`, or the Mac when that is None."""
        player = reply.player if reply is not None and reply.player is not None else player or self._player
        if reply is not None and not self._reply_allowed(reply):
            return self._clock.now()
        self._prune_replies()
        for queued in self._replies.values():
            if queued.player is player:
                queued.trailing_audio_s += clip.seconds
        if reply is not None:
            reply.trailing_audio_s = 0.0
        starts = self._clock.now() + player.pending()
        starts_reply = first = reply is not None and reply.first_audio is None
        if reply is not None:
            if reply.first_audio is None:
                reply.first_audio = starts
            if sentence and reply.first_chunk_audio is None:
                reply.first_chunk_audio = starts
            elif sentence and player.pending() == 0:
                reply.gaps += 1
            reply.audio_s += clip.seconds
        if isinstance(player, RemotePlayer) and reply is not None and reply.reply_id is not None:
            if not player.owns(reply.speech_id):
                if reply.stream_started:
                    reply.speech_id = uuid4().hex
                reply.stream_started = True
                player.begin_reply(reply.reply_id, speech_id=reply.speech_id)
            starts_reply = False
        receipt = player.play(clip, starts_reply=starts_reply)
        if reply is not None and sentence and clip.seconds > 0:
            reply.receipts.append(receipt)
        # The floor observes both its spoken requests and its Announcements on a remote player.
        if (first and reply is not None and (player is self._player or reply.owner is not None or reply.floor)
                and self._on_reply_audio is not None):
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
            "client": reply.client,
            "started_at": reply.began,
            "time_basis": "perf_counter",
            "first_meaningful_audio_ms": ms(reply.first_chunk_audio),
            "meaningful_audio_basis": "queue_estimate",
            "voice": self._tts.voice_for(reply.lang),
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
