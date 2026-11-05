"""Shared helpers for the audio spikes: paths, audio I/O, timing and memory."""

import json
import math
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import psutil
import soundfile as sf
from scipy.signal import resample_poly

SR = 16_000
HERE = Path(__file__).resolve().parent
# The env overrides let a dry run use scratch folders instead of the real ones.
DATA = Path(os.environ.get("JARVIS_AUDIO_DATA", HERE / "data"))
SYNTHETIC = DATA / "synthetic"
RECORDINGS = DATA / "recordings"
# Tracked: numbers, plus transcripts of synthetic speech. Transcripts of the User stay in LOCAL_RESULTS.
RESULTS = Path(os.environ.get("JARVIS_AUDIO_RESULTS", HERE / "results"))
LOCAL_RESULTS = DATA / "results"
TRANSCRIPT_KEYS = {"hyp", "samples", "wrong"}


def prepare_tts() -> None:
    """Use the runtime's bundled-data setup even in the standalone spike environment."""
    sys.path.insert(0, str(HERE.parents[1] / "src"))
    from jarvis_hub.voice.tts import configure_espeak

    configure_espeak()


def load_16k(path: Path) -> np.ndarray:
    audio, sr = sf.read(path, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if sr != SR:
        audio = resample(audio, sr, SR)
    return audio.astype(np.float32)


def resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return audio.astype(np.float32)
    g = math.gcd(src, dst)
    return resample_poly(audio, dst // g, src // g).astype(np.float32)


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


def strip_transcripts(data):
    if isinstance(data, dict):
        return {k: strip_transcripts(v) for k, v in data.items() if k not in TRANSCRIPT_KEYS}
    if isinstance(data, list):
        return [strip_transcripts(v) for v in data]
    return data


def portable(data):
    """Paths under DATA made relative to it and the home folder written as ~, so no result names this machine."""
    if isinstance(data, dict):
        return {k: portable(v) for k, v in data.items()}
    if isinstance(data, list):
        return [portable(v) for v in data]
    if isinstance(data, str):
        return data.replace(f"{DATA}/", "").replace(str(DATA), ".").replace(str(Path.home()), "~")
    return data


def write_result(name: str, data: dict) -> Path:
    """Write `<name>.json` to RESULTS; a result on the User's recordings goes there without transcripts."""
    path = RESULTS / f"{name}.json"
    data = portable(data)
    if data.get("source") == "recordings":
        _write_json(LOCAL_RESULTS / f"{name}.json", data)
        data = strip_transcripts(data)
    _write_json(path, data)
    return path


def full_results_dir(source: str) -> Path:
    """Where results with their transcripts live for this source."""
    return LOCAL_RESULTS if source == "recordings" else RESULTS


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def find_pause(audio: np.ndarray, min_s: float = 0.3) -> tuple[float, float] | None:
    """The longest silence between the first and last speech of a take, in seconds, if at least min_s long."""
    frames = audio[: len(audio) // 160 * 160].reshape(-1, 160)
    if not len(frames):
        return None
    db = 20 * np.log10(np.sqrt((frames**2).mean(axis=1)) + 1e-9)
    floor, peak = np.percentile(db, 10), np.percentile(db, 95)
    # Halfway in dB between room tone and loud speech holds up across mic gain and room noise.
    speech = np.nonzero(db > floor + 0.5 * (peak - floor))[0]
    if len(speech) < 2:
        return None
    gaps = np.diff(speech)
    i = int(np.argmax(gaps))
    if (gaps[i] - 1) * 0.01 < min_s:
        return None
    return round((speech[i] + 1) * 0.01, 2), round(speech[i + 1] * 0.01, 2)


def clips_dir(source: str) -> Path:
    return {"synthetic": SYNTHETIC, "recordings": RECORDINGS}[source]
