"""Audio input: 16 kHz mono in 10 ms frames, through echo cancellation, a pre-roll ring buffer, the Wake word, VAD,
end-of-turn detection, the speaker gate and STT, reported as the voice state machine's input events. While Jarvis
speaks, speech held over it is a Barge-in, with partial transcripts for its decision.

`InputPipeline` is the frame-by-frame logic with every engine injected; `Capture` runs it on a thread of its own,
fed by the `Microphone` or another `FrameSource`.
"""

import asyncio
import queue
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from jarvis_hub.voice.bargein import TALKING_GAP_S, Partial
from jarvis_hub.voice.clips import ClipKind, ClipStore
from jarvis_hub.voice.language import detect_language
from jarvis_hub.voice.player import Samples
from jarvis_hub.voice.state import BargeIn, Heard, InputEvent, Overheard, SpeechStarted, WakeWord

SAMPLE_RATE = 16_000
FRAME = SAMPLE_RATE // 100

Pcm = NDArray[np.int16]

# Frame stamps are sums of 10 ms steps, so a time due on a frame can come out a hair after the frame's stamp.
STAMP_SLACK_S = 0.005
# A Barge-in is decided within 1.3 s; a clip stops growing at 5 s, in case the decision never comes.
MAX_BARGE_CLIP_FRAMES = 500


class EchoCanceller(Protocol):
    def process(self, frame: Pcm) -> Pcm:
        """One 10 ms capture frame, with the echo of what was played taken out. Called on the input thread only."""
        ...

    def reference(self, samples: Samples, rate: int) -> None:
        """Audio as it went to the speakers. Called from the output device's thread."""
        ...


class WakeDetector(Protocol):
    chunk: int

    def score(self, chunk: Pcm) -> float: ...


class VoiceActivity(Protocol):
    chunk: int

    def probability(self, chunk: Pcm) -> float: ...


class TurnDetector(Protocol):
    def complete(self, audio: Samples) -> float:
        """The probability that the User has finished their turn, from the turn's audio."""
        ...


class Transcriber(Protocol):
    def transcribe(self, audio: Samples, *, after: float = 0.0) -> str:
        """The words that end later than `after` seconds into `audio`."""
        ...


class SpeakerGate(Protocol):
    def admits(self, audio: Samples) -> bool:
        """Whether a turn was spoken by the User, so that only the User's turns are transcribed."""
        ...


class EveryoneGate:
    """Admits every turn, until speaker recognition is built."""

    def admits(self, audio: Samples) -> bool:
        return True


@dataclass(frozen=True)
class Engines:
    echo: EchoCanceller
    wake: WakeDetector
    vad: VoiceActivity
    turn: TurnDetector
    stt: Transcriber
    gate: SpeakerGate


@dataclass(frozen=True)
class Listening:
    """Thresholds, each a `JARVIS_*` setting."""

    wake_threshold: float = 0.5
    vad_threshold: float = 0.5
    end_silence_s: float = 0.5
    end_extend_s: float = 1.5
    smart_turn_threshold: float = 0.5
    preroll_s: float = 1.5
    # A turn this long ends whatever VAD says, so a noisy room cannot hold listening open.
    max_turn_s: float = 30.0
    # One phrase scores high on several consecutive windows; later hits within this time are the same phrase.
    wake_refractory_s: float = 2.0
    # VAD fires a little after the true onset, so a turn keeps this much from before it.
    onset_pad_s: float = 0.2
    # Unbroken speech over Jarvis this long is a Barge-in; shorter is a cough or a click.
    bargein_min_speech_s: float = 0.2
    # From a Barge-in's onset, when its speech is transcribed for the decision.
    bargein_partials_s: tuple[float, ...] = (0.4, 0.7)
    # Past the last of those, while the User is still talking, it is transcribed again this often.
    bargein_partial_every_s: float = 0.3
    # Partials start this long before the onset, which VAD finds late, so the first word is whole.
    bargein_lead_s: float = 0.3


@dataclass
class _Turn:
    start: float
    frames: list[Pcm]
    # Smart Turn's verdicts, one per silence that reached `end_silence_s`.
    verdicts: list[float]
    # The Wake word hit that opened the turn, inside its pre-roll; a later "Jarvis" is part of what the User says.
    wake: float | None = None
    judged_silence: float | None = None
    smart_turn_ms: float = 0.0
    assessments: list[dict[str, Any]] = field(default_factory=lambda: [])


class InputPipeline:
    """Push 10 ms frames stamped with the time their last sample was captured; get the events they complete.

    Not thread-safe: one thread pushes frames and turns capture on and off.
    """

    def __init__(
        self,
        engines: Engines,
        settings: Listening,
        record: Callable[[dict[str, Any]], None],
        clock: Callable[[], float] = time.perf_counter,
        clips: ClipStore | None = None,
    ) -> None:
        self._e = engines
        self._s = settings
        self._record = record
        # Times Smart Turn and STT for the metrics.
        self._clock = clock
        self._clips = clips
        self._ring: deque[tuple[Pcm, float]] = deque(maxlen=max(1, round(settings.preroll_s * 100)))
        self._wake_buf = np.zeros(0, np.int16)
        self._vad_buf = np.zeros(0, np.int16)
        self._last_wake = float("-inf")
        # The current stretch of speech, whether or not it is a turn: its onset and the end of its latest speech.
        self._onset: float | None = None
        self._last_speech = 0.0
        self._capturing = False
        self._turn: _Turn | None = None
        # The start of the current unbroken speech, whether it has already made a Barge-in, and whether a resume while
        # it went on has let it make one more, which it may only once, so Backchannels cannot pause and resume forever.
        self._run: float | None = None
        self._run_spent = False
        self._run_rearmed = False
        # Once the run has made its last Barge-in, still transcribed from that onset, without pausing, for interrupts.
        self._overheard: float | None = None
        # From when speech counts toward a Barge-in; None while not watching.
        self._barge_from: float | None = None
        # The onset of the Barge-in under way, when its partials are due, and whether its going quiet was transcribed.
        self._barge: float | None = None
        self._partials_due: list[float] = []
        # When the next partial is due after those, while the User is still talking.
        self._again_at = float("inf")
        self._quiet_told = False
        # The audio of the Barge-in since its onset, for its clip, saved when the state machine next changes what
        # it watches (the decision); None when clips are off or no Barge-in is open. Capped, so a lost call costs little.
        self._clip: tuple[float, list[Pcm]] | None = None
        self._at = 0.0

    def capture(self, on: bool) -> None:
        self._capturing = on
        if not on:
            self._turn = None

    def barge_in(self, since: float | None) -> None:
        """Watch for Barge-ins in speech from `since` on, or stop with None. Either way the one under way is forgotten.

        Its speech raises no other, except once: when watching goes on with the User still talking, as when Jarvis
        resumes over "okay, actually…", the speech held from `since` is a Barge-in of its own. After that second one,
        the rest of the run is only `Overheard`.
        """
        if since is None:
            self._overheard = None
        elif self._barge is not None and self._run is not None:
            if self._run_rearmed:
                self._overheard = self._barge
            else:
                self._run_spent, self._run_rearmed = False, True
        self._save_barge_clip()
        self._barge_from = since
        self._barge = None
        self._partials_due = []

    def push(self, frame: Pcm, at: float) -> list[InputEvent]:
        frame = self._e.echo.process(frame)
        self._ring.append((frame, at))
        self._at = at
        if self._clip is not None and len(self._clip[1]) < MAX_BARGE_CLIP_FRAMES:
            self._clip[1].append(frame)
        if self._turn is not None:
            self._turn.frames.append(frame)
        events: list[InputEvent] = []
        self._wake_buf = np.concatenate([self._wake_buf, frame])
        while len(self._wake_buf) >= self._e.wake.chunk:
            chunk, self._wake_buf = self._wake_buf[: self._e.wake.chunk], self._wake_buf[self._e.wake.chunk :]
            ends = at - len(self._wake_buf) / SAMPLE_RATE
            if self._e.wake.score(chunk) >= self._s.wake_threshold and ends - self._last_wake >= self._s.wake_refractory_s:
                self._last_wake = ends
                events.append(WakeWord(ends))
        self._vad_buf = np.concatenate([self._vad_buf, frame])
        while len(self._vad_buf) >= self._e.vad.chunk:
            chunk, self._vad_buf = self._vad_buf[: self._e.vad.chunk], self._vad_buf[self._e.vad.chunk :]
            ends = at - len(self._vad_buf) / SAMPLE_RATE
            if self._e.vad.probability(chunk) >= self._s.vad_threshold:
                if self._onset is None:
                    self._onset = ends - len(chunk) / SAMPLE_RATE
                if self._run is None:
                    self._run = ends - len(chunk) / SAMPLE_RATE
                self._last_speech = ends
            else:
                self._run, self._run_spent, self._run_rearmed, self._overheard = None, False, False, None
        events += self._barge_events(at)
        if self._onset is not None and self._turn is None and at - self._last_speech >= self._s.end_silence_s:
            self._onset = None
            self._barge = None
        if self._capturing and self._turn is None and self._onset is not None:
            self._turn = self._begin_turn(self._onset)
            events.append(SpeechStarted(at))
        if self._turn is not None and (heard := self._end_of_turn(self._turn, at)) is not None:
            events.append(heard)
        return events

    def _barge_events(self, at: float) -> list[InputEvent]:
        events: list[InputEvent] = []
        since = self._barge_from
        start = None if self._run is None or since is None else max(self._run, since)
        held = start is not None and self._last_speech - start >= self._s.bargein_min_speech_s
        if self._barge is None and start is not None and held and not self._run_spent:
            self._barge, self._run_spent, self._quiet_told = start, True, False
            self._partials_due = [start + offset for offset in self._s.bargein_partials_s]
            self._again_at = start + max(self._s.bargein_partials_s, default=0.0) + self._s.bargein_partial_every_s
            events.append(BargeIn(start, at))
            if self._clips is not None and self._clips.enabled:
                begin = start - self._s.bargein_lead_s - STAMP_SLACK_S
                self._clip = (start, [f for f, t in self._ring if t - 0.01 >= begin])
        if self._barge is None:
            if self._overheard is not None and at >= self._again_at - STAMP_SLACK_S:
                self._again_at += self._s.bargein_partial_every_s
                events.append(Overheard(self._partial(self._overheard, at).text, at))
            return events
        quiet = at - self._last_speech >= TALKING_GAP_S
        due = bool(self._partials_due) and at >= self._partials_due[0] - STAMP_SLACK_S
        while self._partials_due and at >= self._partials_due[0] - STAMP_SLACK_S:
            self._partials_due.pop(0)
        again = not self._partials_due and at >= self._again_at - STAMP_SLACK_S
        if again:
            self._again_at += self._s.bargein_partial_every_s
        # Silero lingers past the end of a word, so a short Backchannel is often still "speech" at 700 ms.
        went_quiet = quiet and not self._quiet_told
        self._quiet_told = quiet
        if due or (again and not quiet) or went_quiet:
            events.append(self._partial(self._barge, at))
        return events

    def _save_barge_clip(self) -> None:
        if self._clip is None or self._clips is None:
            return
        onset, frames = self._clip
        self._clip = None
        self._save(
            "bargein", np.concatenate(frames), {"onset": round(onset, 3), "ended": round(self._at, 3)}
        )

    def _save(self, kind: ClipKind, pcm: Pcm, meta: dict[str, Any]) -> None:
        assert self._clips is not None
        try:
            self._clips.save(kind, pcm, meta)
        except Exception as e:
            self._record({"kind": "clip_error", "error": f"{type(e).__name__}: {e}"})

    def _partial(self, onset: float, at: float) -> Partial:
        start = onset - self._s.bargein_lead_s
        frames = [f for f, t in self._ring if t - 0.01 >= start - STAMP_SLACK_S]
        audio = np.concatenate(frames).astype(np.float32) / 32768.0
        began = self._clock()
        text = self._e.stt.transcribe(audio).strip()
        completed = self._clock()
        self._record({
            "kind": "recognition", "candidate_at": onset, "captured_through_at": at,
            "stt_started_at": began, "stt_completed_at": completed,
            "stt_ms": round((completed - began) * 1000, 1), "time_basis": "perf_counter",
        })
        return Partial(text, at, max(0.0, self._last_speech - onset), at - self._last_speech)

    def _begin_turn(self, onset: float) -> _Turn:
        start = onset - self._s.onset_pad_s
        # A frame's stamp is the time of its last sample, so it starts 10 ms earlier.
        frames = [f for f, t in self._ring if t - 0.01 >= start - STAMP_SLACK_S]
        first = self._ring[-len(frames)][1] - 0.01 if frames else self._ring[-1][1]
        return _Turn(first, frames, [], wake=self._last_wake if self._last_wake > first else None)

    def _end_of_turn(self, turn: _Turn, at: float) -> Heard | None:
        silence = at - self._last_speech
        speaking = self._last_speech > turn.start
        too_long = at - turn.start >= self._s.max_turn_s
        if not too_long and (not speaking or silence < self._s.end_silence_s):
            return None
        if not too_long and turn.judged_silence != self._last_speech:
            turn.judged_silence = self._last_speech
            began = self._clock()
            p = self._e.turn.complete(self._audio(turn))
            completed = self._clock()
            turn.smart_turn_ms += (completed - began) * 1000
            turn.assessments.append({"captured_through_at": at, "started_at": began,
                                     "completed_at": completed, "score": round(p, 3)})
            turn.verdicts.append(round(p, 3))
            if p < self._s.smart_turn_threshold:
                return None
        elif not too_long and silence < self._s.end_silence_s + self._s.end_extend_s:
            return None
        return self._finish(turn, at)

    def _pcm(self, turn: _Turn) -> Pcm:
        n = round((self._last_speech + self._s.end_silence_s - turn.start) * SAMPLE_RATE)
        return np.concatenate(turn.frames)[:n] if turn.frames else np.zeros(0, np.int16)

    def _audio(self, turn: _Turn) -> Samples:
        """The turn from its start to `end_silence_s` past its latest speech: what Smart Turn was trained to judge."""
        return self._pcm(turn).astype(np.float32) / 32768.0

    def _finish(self, turn: _Turn, at: float) -> Heard:
        self._turn = None
        self._onset = None
        audio = self._audio(turn)
        text, stt_ms = "", 0.0
        stt_started: float | None = None
        stt_completed: float | None = None
        admitted = self._e.gate.admits(audio)
        # STT mishears the wake phrase ("A Jarvis", "Hey George"), so it is cut by time: it ended before the hit.
        after = turn.wake - turn.start if turn.wake is not None else 0.0
        if admitted:
            stt_started = self._clock()
            text = self._e.stt.transcribe(audio, after=after).strip()
            stt_completed = self._clock()
            stt_ms = (stt_completed - stt_started) * 1000
        self._record(
            {
                "kind": "turn",
                "time_basis": "perf_counter",
                "request_at": self._last_speech,
                "capture_start_at": turn.start,
                "endpoint_at": at,
                "endpoint_reason": "max_turn" if at - turn.start >= self._s.max_turn_s else "complete",
                "stt_started_at": stt_started,
                "stt_completed_at": stt_completed,
                "assessments": turn.assessments,
                "speech_s": round(self._last_speech - turn.start, 2),
                "end_wait_ms": round((at - self._last_speech) * 1000),
                "smart_turn": turn.verdicts,
                "smart_turn_ms": round(turn.smart_turn_ms, 1),
                "stt_ms": round(stt_ms, 1),
                "words": len(text.split()),
            }
        )
        if self._clips is not None and self._clips.enabled:
            # The language is a guess from the words, so a turn that was not transcribed has none.
            lang = detect_language(text, "en") if text else None
            meta = {"start": round(turn.start, 3), "speech_s": round(self._last_speech - turn.start, 3)}
            self._save("turn", self._pcm(turn), {**meta, "lang": lang, "admitted": admitted})
        return Heard(text, None, self._last_speech)


Push = Callable[[Pcm, float], None]


class FrameSource(Protocol):
    def open(self, push: Push) -> None:
        """Starts calling `push` with each 10 ms frame and the time its last sample was captured, from any thread."""
        ...

    def close(self) -> None: ...


class Microphone:
    """The default input device, 16 kHz mono in 10 ms blocks."""

    def __init__(self) -> None:
        self._stream: Any = None

    def open(self, push: Push) -> None:
        import sounddevice as sd  # pyright: ignore[reportMissingTypeStubs]

        def callback(indata: Any, frames: int, at: Any, status: Any) -> None:
            push(indata[:, 0].copy(), time.perf_counter())

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME, latency="low", callback=callback
        )
        self._stream.start()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()


@dataclass(frozen=True)
class _Capture:
    on: bool


@dataclass(frozen=True)
class _BargeIn:
    since: float | None


# A real microphone never gives exact digital silence for this long; macOS gives it when access is denied.
SILENT_MIC_S = 3.0


class Capture:
    """The pipeline on a frame source, the microphone by default. Engines load and run on a thread of their own,
    since MLX ties a model to the thread that loaded it; `ready` is set once they have loaded and frames flow.

    Frames wait at most `backlog_s` for that thread; past it they are dropped, so a slow engine costs audio, not
    an ever later Wake word and a growing queue.
    """

    def __init__(
        self,
        load: Callable[[], Engines],
        settings: Listening,
        record: Callable[[dict[str, Any]], None],
        notice: Callable[[str], None],
        source: FrameSource | None = None,
        backlog_s: float = 2.0,
        clips: ClipStore | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._clock = clock
        self._trace_frames = 0
        self._max_delay_ms = 0.0
        self._dropped = 0
        self._reference_block = 0
        self._reference_samples = 0
        self._reference_last_report = float("-inf")
        self._clips = clips
        self._load = load
        self._settings = settings
        self._record = record
        self._notice = notice
        self._source: FrameSource = source if source is not None else Microphone()
        # Frames and switches in the order they happened; None stops the thread.
        self._frames: queue.SimpleQueue[tuple[Pcm, float] | _Capture | _BargeIn | None] = queue.SimpleQueue()
        self._max_backlog = round(backlog_s * 100)
        self._backlog = 0
        self._behind = False
        self._backlog_lock = threading.Lock()
        self._events: asyncio.Queue[InputEvent] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Held while the source opens or closes, so a close during loading cannot leave it open.
        self._source_lock = threading.Lock()
        self._closed = False
        # Set once the source is open; until then, and after close, nothing is captured to cancel echo from.
        self._echo: EchoCanceller | None = None
        self.ready = threading.Event()

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        threading.Thread(target=self._run, name="capture", daemon=True).start()

    def capture(self, on: bool) -> None:
        self._frames.put(_Capture(on))

    def barge_in(self, since: float | None) -> None:
        self._frames.put(_BargeIn(since))

    def reference(self, samples: Samples, rate: int) -> None:
        if (echo := self._echo) is not None:
            echo.reference(samples, rate)
            submitted = self._clock()
            self._reference_block += 1
            self._reference_samples += len(samples)
            detailed = self._clips is not None and self._clips.enabled
            if detailed or submitted - self._reference_last_report >= 1.0:
                self._reference_last_report = submitted
                self._on_loop(self._record, {
                    "kind": "playback_reference", "time_basis": "perf_counter",
                    "block": self._reference_block, "submitted_at": submitted,
                    "samples": len(samples), "sample_rate": rate,
                    "total_samples": self._reference_samples, "device_played_at": None,
                })

    async def events(self) -> AsyncIterator[InputEvent]:
        while True:
            yield await self._events.get()

    def close(self) -> None:
        with self._source_lock:
            self._closed = True
            self._echo = None
            self._source.close()
        self._frames.put(None)

    def _on_loop(self, fn: Callable[[Any], None], arg: Any) -> None:
        """Runs `fn` on the event loop's thread, where the Hub's metrics and screen live."""
        assert self._loop is not None
        self._loop.call_soon_threadsafe(fn, arg)

    def _push(self, frame: Pcm, at: float) -> None:
        with self._backlog_lock:
            if self._backlog >= self._max_backlog:
                self._dropped += 1
                if not self._behind:
                    self._behind = True
                    self._on_loop(self._notice, "Listening is falling behind the microphone: dropping audio.")
                return
            self._backlog += 1
        self._frames.put((frame, at))

    def _run(self) -> None:
        began = time.perf_counter()
        try:
            engines = self._load()
            if self._clips is not None:
                try:
                    self._clips.prune()
                except OSError as e:
                    self._clips = None
                    self._on_loop(
                        self._notice, f"Audio clips are off for this run: retention failed: {type(e).__name__}: {e}"
                    )
            with self._source_lock:
                if self._closed:
                    return
                self._source.open(self._push)
                self._echo = engines.echo
        except Exception as e:
            self._on_loop(self._notice, f"Listening is off: {type(e).__name__}: {e}")
            return
        pipeline = InputPipeline(
            engines, self._settings, lambda row: self._on_loop(self._record, row), clock=self._clock, clips=self._clips
        )
        self.ready.set()
        self._on_loop(self._record, {"kind": "ears_loaded", "load_ms": round((time.perf_counter() - began) * 1000)})
        self._on_loop(self._notice, 'Listening for "Hey Jarvis".')
        silent = 0
        while (item := self._frames.get()) is not None:
            if isinstance(item, _Capture):
                pipeline.capture(item.on)
                continue
            if isinstance(item, _BargeIn):
                pipeline.barge_in(item.since)
                continue
            with self._backlog_lock:
                self._backlog -= 1
                # Caught up: a later backlog gets its own notice.
                if self._backlog == 0:
                    self._behind = False
            silent = silent + 1 if not item[0].any() else 0
            if silent == round(SILENT_MIC_S * 100):
                self._on_loop(
                    self._notice,
                    "The microphone gives only silence. If macOS has not allowed it, turn on your terminal app in "
                    "System Settings > Privacy & Security > Microphone, then restart Jarvis.",
                )
            processed = self._clock()
            self._trace_frames += 1
            self._max_delay_ms = max(self._max_delay_ms, (processed - item[1]) * 1000)
            try:
                events = pipeline.push(*item)
            except Exception as e:
                self._on_loop(self._notice, f"Could not process the audio input: {type(e).__name__}: {e}")
                continue
            for event in events:
                self._on_loop(self._events.put_nowait, event)
            with self._backlog_lock:
                backlog = self._backlog
                dropped = self._dropped
                detailed = self._clips is not None and self._clips.enabled
                report = detailed or self._trace_frames >= 100 or (backlog == 0 and (dropped > 0 or self._max_delay_ms >= 100))
                if report:
                    self._dropped = 0
            if report:
                self._on_loop(self._record, {
                    "kind": "capture", "time_basis": "perf_counter",
                    "captured_through_at": item[1], "processed_at": processed,
                    "completed_at": self._clock(), "frames": self._trace_frames,
                    "queued_frames": backlog, "dropped_frames": dropped,
                    "max_queue_delay_ms": round(self._max_delay_ms, 1),
                    "device_captured_at": None,
                })
                self._trace_frames = 0
                self._max_delay_ms = 0.0
