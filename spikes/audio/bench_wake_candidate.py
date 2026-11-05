"""Replay saved wake takes and room negatives through credential-free local candidates."""

import argparse
import hashlib
import importlib.metadata
import json
import time
from pathlib import Path

import numpy as np

from bench_wake import CHUNK, REFRACTORY_S, decode, quiet_seconds
from common import SR, load_16k, rss_mb, write_result
from corpus import WAKE_PHRASINGS


def summarize(positives: dict[str, list[bool]], false_wakes: int, negative_seconds: float) -> dict:
    table = {phrase: {"detected": sum(positives.get(key, [])), "takes": len(positives.get(key, []))}
             for key, phrase in WAKE_PHRASINGS.items()}
    rate = false_wakes * 3600 / negative_seconds if negative_seconds > 0 else None
    return {"per_phrasing": table, "false_wakes": false_wakes,
            "negative_seconds_with_sound": negative_seconds, "false_wakes_per_hour": rate,
            "joint_gate_pass": all(row["takes"] == 20 and row["detected"] >= 19 for row in table.values())
            and negative_seconds >= 3600 and rate is not None and rate <= 1}


class SherpaCandidate:
    def __init__(self, directory: Path, threshold: float, score: float):
        import sentencepiece
        import sherpa_onnx

        tokenizer = sentencepiece.SentencePieceProcessor(model_file=str(directory / "bpe.model"))
        keywords = directory / "jarvis-keywords.txt"
        keywords.write_text(" ".join(tokenizer.encode("JARVIS", out_type=str)) + " @JARVIS\n")
        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=str(directory / "tokens.txt"),
            encoder=str(directory / "encoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
            decoder=str(directory / "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
            joiner=str(directory / "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"),
            keywords_file=str(keywords), num_threads=1,
            keywords_threshold=threshold, keywords_score=score)

    def hits(self, audio: np.ndarray) -> list[float]:
        stream = self.kws.create_stream()
        hits = []
        last = -1e9
        for offset in range(0, len(audio), CHUNK):
            stream.accept_waveform(SR, audio[offset:offset + CHUNK])
            while self.kws.is_ready(stream):
                self.kws.decode_stream(stream)
                if self.kws.get_result(stream):
                    at = (offset + CHUNK) / SR
                    if at - last >= REFRACTORY_S:
                        hits.append(at)
                    last = at
                    self.kws.reset_stream(stream)
        return hits


def evaluate(candidate, recordings: Path) -> dict:
    positives = {key: [] for key in WAKE_PHRASINGS}
    fingerprints = hashlib.sha256()
    compute, duration = 0.0, 0.0
    for key in WAKE_PHRASINGS:
        for path in sorted((recordings / "wake").glob(f"{key}_*.wav")):
            fingerprints.update(path.name.encode() + hashlib.sha256(path.read_bytes()).digest())
            audio = np.concatenate([np.zeros(SR, np.float32), load_16k(path), np.zeros(SR, np.float32)])
            start = time.perf_counter()
            positives[key].append(bool(candidate.hits(audio)))
            compute += time.perf_counter() - start
            duration += len(audio) / SR
    negative_seconds, false_wakes = 0.0, 0
    negatives = sorted((recordings / "room").glob("room_*.wav")) + [recordings / "speaker-run" / "video.wav"]
    for path in negatives:
        fingerprints.update(path.name.encode() + hashlib.sha256(path.read_bytes()).digest())
        pcm = decode(path)
        negative_seconds += len(pcm) / SR - quiet_seconds(pcm)
        start = time.perf_counter()
        false_wakes += len(candidate.hits(np.concatenate([pcm.astype(np.float32) / 32768,
                                                         np.zeros(SR, np.float32)])))
        compute += time.perf_counter() - start
        duration += len(pcm) / SR
    return {"source": "recordings", "purpose": "candidate exploration, not independent confirmation",
            "corpus_sha256": fingerprints.hexdigest(), "negative_files": len(negatives),
            "realtime_factor": compute / duration if duration else None,
            "process_rss_mb": rss_mb(), **summarize(positives, false_wakes, negative_seconds)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recordings", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=0.25)
    parser.add_argument("--score", type=float, default=1.0)
    parser.add_argument("--name", default="wake-candidate-sherpa")
    args = parser.parse_args()
    result = evaluate(SherpaCandidate(args.model, args.threshold, args.score), args.recordings)
    result.update(candidate="sherpa-onnx-gigaspeech-3.3M-fp32-2024-01-01",
                  version=importlib.metadata.version("sherpa-onnx"), threshold=args.threshold,
                  keywords_score=args.score, keyword="JARVIS", threads=1,
                  model_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted(args.model.glob("*.onnx")) if ".int8." not in p.name})
    print(json.dumps(result, indent=2))
    print(write_result(args.name, result))


if __name__ == "__main__":
    main()
