"""Opt-in audio clips of what the microphone heard, for tuning Barge-ins and for speaker recognition later.

A clip is a 16 kHz mono 16-bit WAV with a sidecar JSON of the same name holding its timing and language. They live
under `.jarvis/clips/`, which git ignores, and never go into a log. Off unless `JARVIS_AUDIO_CLIPS=on`; while off,
nothing is written and the folder is not even created.
"""

import json
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

SAMPLE_RATE = 16_000
ClipKind = Literal["bargein", "turn"]


@dataclass(frozen=True)
class ClipSettings:
    """`JARVIS_AUDIO_CLIPS` and its retention: the oldest clips go first once any limit is passed."""

    enabled: bool = False
    max_count: int = 500
    max_days: float = 14.0
    max_mb: float = 500.0


class ClipStore:
    def __init__(self, folder: Path, settings: ClipSettings, now: Callable[[], float] = time.time) -> None:
        self._folder = folder
        self._s = settings
        self._now = now

    @property
    def enabled(self) -> bool:
        return self._s.enabled

    def save(self, kind: ClipKind, samples: NDArray[np.int16], meta: dict[str, Any]) -> Path | None:
        """Writes the clip and its sidecar, then prunes; None while clips are off. `meta` goes in the sidecar."""
        if not self._s.enabled:
            return None
        self._folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(self._now(), timezone.utc)
        name = f"{kind}-{stamp.strftime('%Y%m%dT%H%M%S')}-{stamp.microsecond // 1000:03d}"
        wav = self._folder / f"{name}.wav"
        with wave.open(str(wav), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(SAMPLE_RATE)
            f.writeframes(samples.astype("<i2").tobytes())
        sidecar = {
            "kind": kind,
            "at": stamp.isoformat(timespec="milliseconds"),
            "rate": SAMPLE_RATE,
            "channels": 1,
            "duration_s": round(len(samples) / SAMPLE_RATE, 3),
            **meta,
        }
        wav.with_suffix(".json").write_text(json.dumps(sidecar, ensure_ascii=False))
        self.prune()
        return wav

    def prune(self) -> None:
        """Deletes the oldest clips past the age, count and total size limits."""
        if not self._s.enabled or not self._folder.is_dir():
            return
        clips: list[tuple[float, int, list[Path]]] = []
        for wav in self._folder.glob("*.wav"):
            files = [p for p in (wav, wav.with_suffix(".json")) if p.exists()]
            try:
                stats = [p.stat() for p in files]
            except OSError:
                continue
            clips.append((stats[0].st_mtime, sum(s.st_size for s in stats), files))
        clips.sort(key=lambda clip: clip[0])
        oldest_kept = self._now() - self._s.max_days * 86_400
        total = sum(size for _, size, _ in clips)
        for mtime, size, files in list(clips):
            if mtime >= oldest_kept and len(clips) <= self._s.max_count and total <= self._s.max_mb * 1_000_000:
                break
            for path in files:
                path.unlink(missing_ok=True)
            clips.pop(0)
            total -= size
