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
    def play(self, clip: Clip) -> None:
        """Queue a clip to start the moment the one before it ends."""
        ...

    def pause(self) -> None:
        """Fade out and hold the place."""
        ...

    def resume(self, rewind_ms: int = 300) -> None:
        """Go on from a little before where the pause left off, fading back in."""
        ...

    def stop(self) -> None:
        """Fade out and drop everything queued."""
        ...

    def played(self) -> str:
        """The text heard since Jarvis last started speaking from silence, up to the word playing now."""
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


class SoundPlayer:
    """A player on the default output device. The device's audio thread pulls samples through `_fill`."""

    def __init__(self, sample_rate: int, *, open_stream: OpenStream = _DeviceStream, fade_ms: int = 20) -> None:
        self._rate = sample_rate
        self._fade = max(1, sample_rate * fade_ms // 1000)
        self._lock = threading.Lock()
        self._clips: deque[Clip] = deque()
        self._pos = 0
        self._heard: list[str] = []
        self._gain = 1.0
        self._target = 1.0
        self._flush_when_silent = False
        self._stream = open_stream(sample_rate, self._fill)

    def play(self, clip: Clip) -> None:
        with self._lock:
            if not self._clips:
                self._heard = []
            self._clips.append(clip)

    def pause(self) -> None:
        with self._lock:
            self._target = 0.0

    def resume(self, rewind_ms: int = 300) -> None:
        with self._lock:
            if self._target == 1.0 or self._flush_when_silent:
                return
            self._pos = max(0, self._pos - self._rate * rewind_ms // 1000)
            self._target = 1.0

    def stop(self) -> None:
        with self._lock:
            self._target = 0.0
            self._flush_when_silent = True
            if self._gain == 0.0 or not self._clips:
                self._flush()

    def played(self) -> str:
        with self._lock:
            heard = list(self._heard)
            if self._clips and self._pos:
                current = self._clips[0]
                words = current.text.split()
                heard.append(" ".join(words[: len(words) * self._pos // len(current.samples)]))
            return " ".join(h for h in heard if h)

    def pending(self) -> float:
        with self._lock:
            return (sum(len(c.samples) for c in self._clips) - self._pos) / self._rate

    def close(self) -> None:
        self._stream.close()

    def _flush(self) -> None:
        if self._clips and self._pos:
            current = self._clips[0]
            words = current.text.split()
            self._heard.append(" ".join(words[: len(words) * self._pos // len(current.samples)]))
        self._clips.clear()
        self._pos = 0
        self._gain = self._target = 1.0
        self._flush_when_silent = False

    def _fill(self, out: Samples) -> None:
        out[:] = 0
        with self._lock:
            if self._gain == 0.0 and self._target == 0.0:
                return
            n = 0
            while n < len(out) and self._clips:
                clip = self._clips[0]
                take = min(len(out) - n, len(clip.samples) - self._pos)
                out[n : n + take] = clip.samples[self._pos : self._pos + take]
                self._pos += take
                n += take
                if self._pos == len(clip.samples):
                    self._heard.append(clip.text)
                    self._clips.popleft()
                    self._pos = 0
            if self._gain != self._target:
                self._ramp(out, n)

    def _ramp(self, out: Samples, n: int) -> None:
        """Moves the gain towards its target by one fade's worth over the samples; stops where silence is reached."""
        step = 1.0 / self._fade
        if self._target < self._gain:
            gains = np.maximum(self._gain - step * np.arange(1, len(out) + 1), 0.0)
        else:
            gains = np.minimum(self._gain + step * np.arange(len(out)), 1.0)
        out *= gains.astype(np.float32)
        self._gain = float(gains[-1])
        if self._gain > 0.0:
            return
        silent_from = int(np.argmax(gains == 0.0))
        # Samples taken past the fade were never heard: give them back.
        self._pos -= min(self._pos, max(0, n - silent_from - 1))
        if self._flush_when_silent:
            self._flush()
