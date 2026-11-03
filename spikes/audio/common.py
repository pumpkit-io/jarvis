"""Shared helpers for the audio spikes: paths, audio I/O, timing and memory."""

import json
import os
import statistics
import subprocess
import time
from pathlib import Path

import numpy as np
import psutil
import soundfile as sf

SR = 16_000
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
SYNTHETIC = DATA / "synthetic"
RECORDINGS = DATA / "recordings"
RESULTS = DATA / "results"


def load_16k(path: Path) -> np.ndarray:
    audio, sr = sf.read(path, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if sr != SR:
        audio = resample(audio, sr, SR)
    return audio.astype(np.float32)


def resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return audio
    n = int(round(len(audio) * dst / src))
    return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)


def save_16k(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, audio, SR, subtype="PCM_16")


def rss_mb(pid: int | None = None) -> float:
    return psutil.Process(pid or os.getpid()).memory_info().rss / 2**20


def footprint_mb(pid: int | None = None) -> float | None:
    """macOS phys_footprint, which counts GPU (Metal) buffers that RSS misses for MLX."""
    try:
        out = subprocess.run(["footprint", "-p", str(pid or os.getpid())], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        if "Footprint:" in line and "phys_footprint" not in line:
            value, unit = line.split("Footprint:")[1].split()[:2]
            return float(value) * {"KB": 1 / 1024, "MB": 1, "GB": 1024}[unit]
    return None


def timed(fn, *args, runs: int = 5, warmup: int = 1, **kwargs) -> tuple[list[float], object]:
    for _ in range(warmup):
        fn(*args, **kwargs)
    times, result = [], None
    for _ in range(runs):
        t0 = time.perf_counter()
        result = fn(*args, **kwargs)
        times.append((time.perf_counter() - t0) * 1000)
    return times, result


def p50(xs: list[float]) -> float:
    return statistics.median(xs)


def write_result(name: str, data: dict) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return path


def clips_dir(source: str) -> Path:
    return {"synthetic": SYNTHETIC, "recordings": RECORDINGS}[source]
