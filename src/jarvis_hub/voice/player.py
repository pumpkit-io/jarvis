"""Plays synthesised clips back to back, and can pause, resume and stop mid-sentence."""

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

Samples = NDArray[np.float32]


@dataclass(frozen=True)
class Clip:
    """Mono float samples and the text they say."""

    text: str
    voice: str
    samples: Samples
    sample_rate: int

    @property
    def seconds(self) -> float:
        return len(self.samples) / self.sample_rate


class Player(Protocol):
    def play(self, clip: Clip, *, starts_reply: bool = False) -> None:
        """Queue a clip to start the moment the one before it ends; `starts_reply` starts a new `played` record."""
        ...

    def pause(self) -> None:
        """Fade out and hold the place."""
        ...

    def resume(self, rewind_ms: int = 300) -> None:
        """Go on from a little before where the pause left off, fading back in."""
        ...

    def stop(self) -> None:
        """Fade out and drop everything queued so far."""
        ...

    def played(self) -> str:
        """The text of the current reply heard so far, up to the word playing now."""
        ...

    def pending(self) -> float:
        """Seconds of queued audio not yet played."""
        ...

    def close(self) -> None: ...


class Stream(Protocol):
    def close(self) -> None: ...


# Opens the output at this sample rate, calling the function to fill each block of mono samples.
OpenStream = Callable[[int, Callable[[Samples], None]], Stream]


class _DeviceStream:
    def __init__(self, sample_rate: int, fill: Callable[[Samples], None]) -> None:
        # Imported here, so text mode without speech never opens the audio system.
        import sounddevice as sd  # pyright: ignore[reportMissingTypeStubs]

        def callback(outdata: Any, frames: int, time: Any, status: Any) -> None:
            fill(outdata[:, 0])

        self._stream = sd.OutputStream(
            samplerate=sample_rate, channels=1, dtype="float32", latency="low", callback=callback
        )
        self._stream.start()

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()


@dataclass
class _Queued:
    clip: Clip
    starts_reply: bool


class SoundPlayer:
    """A player on the default output device. The device's audio thread pulls samples through `_fill`."""

    def __init__(self, sample_rate: int, *, open_stream: OpenStream = _DeviceStream, fade_ms: int = 20) -> None:
        self._rate = sample_rate
        self._fade = max(1, sample_rate * fade_ms // 1000)
        self._lock = threading.Lock()
        self._clips: deque[_Queued] = deque()
        self._pos = 0
        # The clip that finished last, so a resume can rewind into it.
        self._previous: _Queued | None = None
        self._heard: list[str] = []
        self._gain = 1.0
        self._target = 1.0
        # A stop drops this many clips at the head of the queue once its fade reaches silence.
        self._stopping = False
        self._to_flush = 0
        self._stream = open_stream(sample_rate, self._fill)

    def play(self, clip: Clip, *, starts_reply: bool = False) -> None:
        with self._lock:
            self._clips.append(_Queued(clip, starts_reply))
            if len(self._clips) == 1 and self._pos == 0:
                self._start_current()

    def pause(self) -> None:
        with self._lock:
            self._target = 0.0

    def resume(self, rewind_ms: int = 300) -> None:
        with self._lock:
            if self._target == 1.0 or self._stopping:
                return
            self._pos -= self._rate * rewind_ms // 1000
            if self._pos < 0 and self._previous is not None and self._clips:
                self._clips.appendleft(self._previous)
                self._heard.pop()
                self._pos += len(self._previous.clip.samples)
                self._previous = None
            self._pos = max(0, self._pos)
            self._target = 1.0

    def stop(self) -> None:
        with self._lock:
            self._target = 0.0
            self._stopping = True
            self._to_flush = len(self._clips)
            if self._gain == 0.0 or not self._clips:
                self._flush()

    def played(self) -> str:
        with self._lock:
            return " ".join(h for h in [*self._heard, self._partial()] if h)

    def pending(self) -> float:
        with self._lock:
            return (sum(len(q.clip.samples) for q in self._clips) - self._pos) / self._rate

    def close(self) -> None:
        self._stream.close()

    def _partial(self) -> str:
        """The words of the playing clip heard so far, in proportion to the samples played."""
        if not self._clips or not self._pos:
            return ""
        current = self._clips[0].clip
        words = current.text.split()
        return " ".join(words[: len(words) * self._pos // len(current.samples)])

    def _start_current(self) -> None:
        if self._clips[0].starts_reply:
            self._heard = []
            self._previous = None

    def _flush(self) -> None:
        if self._to_flush:
            self._heard.append(self._partial())
            self._pos = 0
        for _ in range(self._to_flush):
            self._clips.popleft()
        self._previous = None
        self._gain = self._target = 1.0
        self._stopping = False
        self._to_flush = 0
        if self._clips:
            self._start_current()

    def _fill(self, out: Samples) -> None:
        out[:] = 0
        with self._lock:
            if self._gain == 0.0 and self._target == 0.0:
                return
            gains = None
            limit = len(out)
            if self._gain != self._target:
                gains = self._gains(len(out))
                if self._target < self._gain:
                    # Past the fade nothing is heard, so those samples stay queued.
                    limit = int(np.count_nonzero(gains > 0.0))
            n = 0
            while n < limit and self._clips:
                clip = self._clips[0].clip
                take = min(limit - n, len(clip.samples) - self._pos)
                out[n : n + take] = clip.samples[self._pos : self._pos + take]
                self._pos += take
                n += take
                if self._pos == len(clip.samples):
                    self._heard.append(clip.text)
                    self._previous = self._clips.popleft()
                    self._to_flush = max(0, self._to_flush - 1)
                    self._pos = 0
                    if self._clips:
                        self._start_current()
            if gains is not None:
                out *= gains.astype(np.float32)
                self._gain = float(gains[-1])
                if self._gain == 0.0 and self._stopping:
                    self._flush()

    def _gains(self, frames: int) -> NDArray[np.float64]:
        """The gain for each frame, one fade's worth of change from the current gain towards the target."""
        step = 1.0 / self._fade
        if self._target < self._gain:
            return np.maximum(self._gain - step * np.arange(1, frames + 1), 0.0)
        return np.minimum(self._gain + step * np.arange(frames), 1.0)
