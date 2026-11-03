"""Wake word: openWakeWord `hey_jarvis` recall per phrasing and false accepts per hour.

    uv run bench_wake.py recall --source synthetic
    uv run bench_wake.py false-accepts <audio files or folders> [--hours 6]

Porcupine is not run: it needs a Picovoice AccessKey, which means an account.
"""

import argparse
import json
import subprocess
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import openwakeword.utils
from openwakeword.model import Model

from common import SR, clips_dir, load_16k, write_result
from corpus import WAKE_PHRASINGS

CHUNK = 1280  # 80 ms, the frame size openWakeWord is trained on
THRESHOLDS = [0.3, 0.5, 0.7]
VAD_THRESHOLDS = [0.0, 0.5]
REFRACTORY_S = 2.0
QUIET_DBFS = -55
AUDIO_EXT = {".wav", ".flac", ".mp3", ".m4a", ".mp4", ".mov", ".aiff"}


def model(vad: float) -> Model:
    openwakeword.utils.download_models(["hey_jarvis"])
    return Model(wakeword_models=["hey_jarvis"], inference_framework="onnx", vad_threshold=vad)


def scores(m: Model, pcm: np.ndarray) -> np.ndarray:
    m.reset()
    out = []
    for i in range(0, len(pcm) - CHUNK + 1, CHUNK):
        out.append(m.predict(pcm[i : i + CHUNK])["hey_jarvis"])
    return np.array(out)


def to_int16(audio: np.ndarray) -> np.ndarray:
    return (np.clip(audio, -1, 1) * 32767).astype(np.int16)


def recall(source: str) -> dict:
    takes = sorted((clips_dir(source) / "wake").glob("*.wav"))
    # openWakeWord needs about a second of audio before its scores settle, and session takes start on the cue.
    pad = np.zeros(SR, np.float32)
    pcm = {t.stem: to_int16(np.concatenate([pad, load_16k(t), pad[: SR // 2]])) for t in takes}
    result = {"source": source, "takes": len(takes), "by_vad": {}}
    for vad in VAD_THRESHOLDS:
        m = model(vad)
        peaks = {name: float(scores(m, audio).max()) for name, audio in pcm.items()}
        table = {}
        for key, phrase in WAKE_PHRASINGS.items():
            mine = [p for n, p in peaks.items() if n.startswith(f"{key}_")]
            table[phrase] = {f"recall@{t}": f"{sum(p >= t for p in mine)}/{len(mine)}" for t in THRESHOLDS}
            table[phrase]["peak_median"] = round(float(np.median(mine)), 3) if mine else None
        result["by_vad"][f"vad_threshold={vad}"] = {"per_phrasing": table, "peaks": {k: round(v, 3) for k, v in peaks.items()}}
    return result


def decode(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16)


def false_accepts(paths: list[Path], max_hours: float) -> dict:
    files = []
    for p in paths:
        files += sorted(f for f in p.rglob("*") if f.suffix.lower() in AUDIO_EXT) if p.is_dir() else [p]
    models = {vad: model(vad) for vad in VAD_THRESHOLDS}
    hits = {vad: {t: 0 for t in THRESHOLDS} for vad in VAD_THRESHOLDS}
    examples = defaultdict(list)
    seconds, quiet, compute = 0.0, 0.0, 0.0
    for f in files:
        if seconds >= max_hours * 3600:
            break
        pcm = decode(f)
        seconds += len(pcm) / SR
        # A file near silence means the video had stopped, so those minutes tested nothing.
        if 20 * np.log10(np.sqrt(np.mean((pcm / 32768.0) ** 2)) + 1e-9) < QUIET_DBFS:
            quiet += len(pcm) / SR
        for vad, m in models.items():
            t0 = time.perf_counter()
            s = scores(m, pcm)
            compute += time.perf_counter() - t0 if vad == 0.0 else 0
            for t in THRESHOLDS:
                last = -1e9
                for i in np.nonzero(s >= t)[0]:
                    at = i * CHUNK / SR
                    if at - last >= REFRACTORY_S:
                        hits[vad][t] += 1
                        if t == 0.5 and len(examples[vad]) < 10:
                            examples[vad].append(f"{f.name}@{at:.1f}s ({s[i]:.2f})")
                    last = at
    hours = seconds / 3600
    return {
        "files": len(files), "hours": round(hours, 2), "quiet_minutes": round(quiet / 60, 1),
        "realtime_factor_no_vad": round(compute / seconds, 4) if seconds else None,
        "false_accepts_per_hour": {f"vad_threshold={v}": {f"@{t}": round(n / hours, 2) for t, n in h.items()} for v, h in hits.items()},
        "false_accepts_total": {f"vad_threshold={v}": h for v, h in hits.items()},
        "examples_at_0.5": {f"vad_threshold={v}": e for v, e in examples.items()},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("recall")
    r.add_argument("--source", choices=["synthetic", "recordings"], default="synthetic")
    fa = sub.add_parser("false-accepts")
    fa.add_argument("paths", nargs="+", type=Path)
    fa.add_argument("--hours", type=float, default=6.0)
    fa.add_argument("--name", default="false-accepts")
    args = ap.parse_args()
    if args.cmd == "recall":
        result = recall(args.source)
        name = f"wake-recall-{args.source}"
        shown = {k: {kk: vv["per_phrasing"] for kk, vv in v.items()} if k == "by_vad" else v for k, v in result.items()}
    else:
        result = false_accepts(args.paths, args.hours)
        name = f"wake-{args.name}"
        shown = result
    print(json.dumps(shown, indent=2))
    print(f"-> {write_result(name, result)}")


if __name__ == "__main__":
    main()
