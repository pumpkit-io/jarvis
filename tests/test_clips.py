"""Audio clips: opt-in, a plain 16 kHz mono WAV with a sidecar, and retention."""

import json
import os
import time
import wave
from pathlib import Path

import numpy as np

from jarvis_hub.voice.clips import ClipSettings, ClipStore

# Files carry their real modification times, so the store's clock must agree with the file system's.
NOW = time.time()
SECOND = np.zeros(16_000, dtype=np.int16)


def store(tmp_path: Path, **settings: object) -> ClipStore:
    return ClipStore(tmp_path / "clips", ClipSettings(enabled=True, **settings), now=lambda: NOW)  # pyright: ignore[reportArgumentType]


def clips(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in (tmp_path / "clips").glob("*.wav"))


def age(tmp_path: Path, name: str, days: float) -> None:
    for suffix in (".wav", ".json"):
        path = tmp_path / "clips" / (name.removesuffix(".wav") + suffix)
        os.utime(path, (NOW - days * 86_400, NOW - days * 86_400))


def test_clips_are_off_by_default_and_write_nothing_not_even_the_folder(tmp_path: Path) -> None:
    off = ClipStore(tmp_path / "clips", ClipSettings(), now=lambda: NOW)

    assert off.save("turn", SECOND, {"lang": "en"}) is None
    off.prune()

    assert not (tmp_path / "clips").exists()


def test_a_clip_is_16_khz_mono_wav_with_a_sidecar_of_its_timing_and_language(tmp_path: Path) -> None:
    wav = store(tmp_path).save("turn", np.arange(8_000, dtype=np.int16), {"start": 3.5, "lang": "it"})

    assert wav is not None and wav.name.startswith("turn-")
    with wave.open(str(wav)) as f:
        assert (f.getframerate(), f.getnchannels(), f.getsampwidth(), f.getnframes()) == (16_000, 1, 2, 8_000)
        assert np.frombuffer(f.readframes(8_000), dtype="<i2")[:3].tolist() == [0, 1, 2]
    sidecar = json.loads(wav.with_suffix(".json").read_text())
    assert (sidecar["kind"], sidecar["rate"], sidecar["duration_s"], sidecar["start"], sidecar["lang"]) == (
        "turn",
        16_000,
        0.5,
        3.5,
        "it",
    )


def at(tmp_path: Path, i: int, **settings: object) -> ClipStore:
    """A store whose clock is `i` seconds on, so each clip it writes has a name of its own."""
    return ClipStore(tmp_path / "clips", ClipSettings(enabled=True, **settings), now=lambda: NOW + i)  # pyright: ignore[reportArgumentType]


def test_the_oldest_clips_go_first_once_there_are_more_than_the_count_limit(tmp_path: Path) -> None:
    written = [at(tmp_path, i, max_count=2).save("bargein", SECOND, {}) for i in range(3)]

    assert clips(tmp_path) == sorted(w.name for w in written[1:] if w is not None)
    assert len(list((tmp_path / "clips").glob("*.json"))) == 2


def test_clips_older_than_the_day_limit_are_pruned_with_their_sidecars(tmp_path: Path) -> None:
    old, fresh = at(tmp_path, 0, max_days=7).save("turn", SECOND, {}), at(tmp_path, 1, max_days=7).save("turn", SECOND, {})
    assert old is not None and fresh is not None
    age(tmp_path, old.name, 8)
    age(tmp_path, fresh.name, 6)

    store(tmp_path, max_days=7).prune()

    assert clips(tmp_path) == [fresh.name]
    assert not old.with_suffix(".json").exists()


def test_the_oldest_clips_go_when_the_total_size_passes_the_limit(tmp_path: Path) -> None:
    # Each clip is 32 kB of audio; the limit is 70 kB.
    written = [at(tmp_path, i, max_mb=0.07).save("turn", SECOND, {}) for i in range(3)]

    assert len(clips(tmp_path)) == 2
    assert written[0] is not None and not written[0].exists()


def test_pruning_on_startup_applies_the_limits_to_clips_left_by_an_earlier_run(tmp_path: Path) -> None:
    for i in range(4):
        at(tmp_path, i).save("turn", SECOND, {})
    assert len(clips(tmp_path)) == 4

    store(tmp_path, max_count=1).prune()

    assert len(clips(tmp_path)) == 1
