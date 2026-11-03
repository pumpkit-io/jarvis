"""Smart Turn v3: inference time on the M3, its verdicts on complete and cut-off turns, and what they cost.

    uv run bench_smart_turn.py --source synthetic

A complete turn ends at the clip's end. A cut-off turn is a long clip cut at the
quietest word gap near its middle, which is what a mid-sentence pause looks like to VAD.
The pause takes (`PAUSE_TAKES`) add real pauses: each is cut 200 ms into its pause.

The end-of-turn saving follows the Spec's input pipeline: after VAD_SILENCE_MS of silence
Smart Turn runs; "complete" ends the turn, "incomplete" keeps listening up to EXTENSION_MS more.
"""

import argparse
import json
import time
from pathlib import Path

import numpy as np

from common import SR, clips_dir, find_pause, load_16k, p50, rss_mb, write_result
from corpus import CLIPS, PAUSE_TAKES

MODELS = ["smart-turn-v3.2-cpu.onnx", "smart-turn-v3.0.onnx"]
VAD_SILENCE_MS = 500
EXTENSION_MS = 1500
FIXED_THRESHOLDS_MS = [800, 1000, 1500]


class SmartTurn:
    def __init__(self, filename: str):
        import onnxruntime as ort
        from huggingface_hub import snapshot_download
        from transformers import WhisperFeatureExtractor

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


def end_of_turn(p_complete: dict, p_pause: dict, pause_s: dict, infer_ms: float) -> dict:
    """End-of-turn latency on complete turns and premature cuts on real pauses, from the model's verdicts.

    A complete turn judged incomplete waits the whole extension, so the saving is a lower bound.
    """
    ends = [VAD_SILENCE_MS + infer_ms + (0 if p > 0.5 else EXTENSION_MS) for p in p_complete.values()]
    mean = sum(ends) / len(ends) if ends else None
    pauses = {k: pause_s[k] * 1000 for k in p_pause if pause_s.get(k) is not None}

    def cut_by_smart_turn(k):
        return pauses[k] >= VAD_SILENCE_MS and (p_pause[k] > 0.5 or pauses[k] >= VAD_SILENCE_MS + infer_ms + EXTENSION_MS)

    premature = {"smart_turn": f"{sum(map(cut_by_smart_turn, pauses))}/{len(pauses)}"}
    premature |= {f"fixed_{t}ms": f"{sum(ms >= t for ms in pauses.values())}/{len(pauses)}" for t in FIXED_THRESHOLDS_MS}
    return {
        "complete_judged_incomplete": f"{sum(p <= 0.5 for p in p_complete.values())}/{len(p_complete)}",
        "end_ms_mean": None if mean is None else round(mean),
        "saved_ms_vs_fixed_silence": {t: None if mean is None else round(t - mean) for t in FIXED_THRESHOLDS_MS},
        "premature_cuts": premature,
    }


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
            "p_complete": p_complete, "p_cut": p_cut,
        }
        p_pause = {k: round(model(a), 3) for k, a in pause_cut.items()}
        p_whole = {k: round(model(a), 3) for k, a in pause_complete.items()}
        if pause_cut or pause_complete:
            result["models"][filename] |= {
                "real_pause_judged_incomplete": f"{sum(p <= 0.5 for p in p_pause.values())}/{len(p_pause)}",
                "pause_takes_judged_complete": f"{sum(p > 0.5 for p in p_whole.values())}/{len(p_whole)}",
                "p_real_pause": p_pause, "p_pause_take_complete": p_whole,
            }
        # Complete turns are the clips and the whole pause takes; premature cuts come from the real pauses only.
        result["models"][filename]["end_of_turn"] = end_of_turn(p_complete | p_whole, p_pause, pauses, infer)
    result["rss_mb_with_both_models"] = round(rss_mb())
    print(json.dumps({k: ({m: {kk: vv for kk, vv in v.items() if not kk.startswith("p_")} for m, v in r.items()}
                          if k == "models" else r) for k, r in result.items()}, indent=2))
    print(f"-> {write_result(f'smart-turn-{args.source}', result)}")


if __name__ == "__main__":
    main()
