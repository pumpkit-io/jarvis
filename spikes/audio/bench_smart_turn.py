"""Smart Turn v3: inference time on the M3 and its verdicts on complete and cut-off turns.

    uv run bench_smart_turn.py --source synthetic

A complete turn ends at the clip's end. A cut-off turn is a long clip cut at the
quietest word gap near its middle, which is what a mid-sentence pause looks like to VAD.
The pause takes (`PAUSE_TAKES`) add real pauses: each is cut 200 ms into its pause.
"""

import argparse
import json
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from huggingface_hub import snapshot_download
from transformers import WhisperFeatureExtractor

from common import SR, clips_dir, find_pause, load_16k, p50, rss_mb, write_result
from corpus import CLIPS, PAUSE_TAKES

MODELS = ["smart-turn-v3.2-cpu.onnx", "smart-turn-v3.0.onnx"]
VAD_SILENCE_MS = 500
FIXED_THRESHOLDS_MS = [800, 1000, 1500]


class SmartTurn:
    def __init__(self, filename: str):
        path = Path(snapshot_download("pipecat-ai/smart-turn-v3")) / filename
        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(str(path), sess_options=so)
        self.features = WhisperFeatureExtractor(chunk_length=8)
        self.size_mb = round(path.stat().st_size / 2**20, 1)

    def __call__(self, audio: np.ndarray) -> float:
        # The model is trained on the last 8 s with zeros padded at the start, not the end.
        audio = audio[-8 * SR :]
        audio = np.pad(audio, (8 * SR - len(audio), 0))
        inputs = self.features(audio, sampling_rate=SR, return_tensors="np", padding="max_length",
                               max_length=8 * SR, truncation=True, do_normalize=True)
        feats = inputs.input_features.astype(np.float32)
        return float(self.session.run(None, {"input_features": feats})[0][0].item())


def trim_tail(audio: np.ndarray) -> np.ndarray:
    frames = audio[: len(audio) // 160 * 160].reshape(-1, 160)
    loud = np.nonzero(np.sqrt((frames**2).mean(axis=1)) > 0.01)[0]
    return audio[: (loud[-1] + 1) * 160] if len(loud) else audio


def cut_mid_sentence(audio: np.ndarray) -> np.ndarray:
    frames = audio[: len(audio) // 160 * 160].reshape(-1, 160)
    energy = np.sqrt((frames**2).mean(axis=1))
    n = len(energy)
    window = range(int(n * 0.35), int(n * 0.65))
    gap = min(window, key=lambda i: energy[max(0, i - 3) : i + 3].mean())
    return audio[: gap * 160]


def pause_turns(folder: Path) -> tuple[dict, dict, dict]:
    """Cuts at each real pause, the whole pause takes as complete turns, and every pause's length."""
    cut, complete, pauses = {}, {}, {}
    for take in PAUSE_TAKES:
        path = folder / f"{take.id}.wav"
        if not path.exists():
            continue
        audio = load_16k(path)
        complete[take.id] = np.concatenate([trim_tail(audio), np.zeros(int(0.2 * SR), np.float32)])
        if take.paused:
            pause = find_pause(audio)
            pauses[take.id] = None if pause is None else round(pause[1] - pause[0], 2)
            if pause:
                cut[take.id] = audio[: int((pause[0] + 0.2) * SR)]
    return cut, complete, pauses


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["synthetic", "recordings"], default="synthetic")
    args = ap.parse_args()
    folder = clips_dir(args.source) / "clips"
    clips = [c for c in CLIPS if (folder / f"{c.id}.wav").exists()]
    silence = np.zeros(int(0.2 * SR), np.float32)
    complete = {c.id: np.concatenate([trim_tail(load_16k(folder / f"{c.id}.wav")), silence]) for c in clips}
    cut = {c.id: np.concatenate([cut_mid_sentence(trim_tail(load_16k(folder / f"{c.id}.wav"))), silence])
           for c in clips if c.kind == "long"}

    pause_cut, pause_complete, pauses = pause_turns(clips_dir(args.source) / "pauses")
    result = {"source": args.source, "models": {}, "pause_s": pauses}
    for filename in MODELS:
        model = SmartTurn(filename)
        model(complete[clips[0].id])
        times = []
        for audio in list(complete.values()) * 3:
            t = time.perf_counter()
            model(audio)
            times.append((time.perf_counter() - t) * 1000)
        p_complete = {k: round(model(a), 3) for k, a in complete.items()}
        p_cut = {k: round(model(a), 3) for k, a in cut.items()}
        tp = sum(p > 0.5 for p in p_complete.values())
        tn = sum(p <= 0.5 for p in p_cut.values())
        infer = p50(times)
        result["models"][filename] = {
            "size_mb": model.size_mb,
            "inference_ms_p50": round(infer, 1), "inference_ms_max": round(max(times), 1),
            "complete_judged_complete": f"{tp}/{len(p_complete)}",
            "cut_judged_incomplete": f"{tn}/{len(p_cut)}",
            "end_of_turn_ms_with_smart_turn": round(VAD_SILENCE_MS + infer),
            "saved_ms_vs_fixed_silence": {t: round(t - VAD_SILENCE_MS - infer) for t in FIXED_THRESHOLDS_MS},
            "p_complete": p_complete, "p_cut": p_cut,
        }
        if pause_cut or pause_complete:
            p_pause = {k: round(model(a), 3) for k, a in pause_cut.items()}
            p_whole = {k: round(model(a), 3) for k, a in pause_complete.items()}
            result["models"][filename] |= {
                "real_pause_judged_incomplete": f"{sum(p <= 0.5 for p in p_pause.values())}/{len(p_pause)}",
                "pause_takes_judged_complete": f"{sum(p > 0.5 for p in p_whole.values())}/{len(p_whole)}",
                "p_real_pause": p_pause, "p_pause_take_complete": p_whole,
            }
    result["rss_mb_with_both_models"] = round(rss_mb())
    print(json.dumps({k: ({m: {kk: vv for kk, vv in v.items() if not kk.startswith("p_")} for m, v in r.items()}
                          if k == "models" else r) for k, r in result.items()}, indent=2))
    print(f"-> {write_result(f'smart-turn-{args.source}', result)}")


if __name__ == "__main__":
    main()
