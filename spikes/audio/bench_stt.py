"""STT: latency on ~2 s and ~6 s clips, partials on 0.4-0.8 s snippets, WER and memory.

    uv run bench_stt.py --source synthetic            # every engine, one subprocess each
    uv run bench_stt.py --source recordings --engine parakeet

:2023 is measured only as the server whose pid --turbo-pid names, so a stray listener never becomes the turbo row.
"""

import argparse
import io
import json
import re
import subprocess
import sys
import time
import unicodedata

import httpx
import jiwer
import numpy as np
import soundfile as sf

from common import HERE, SR, clips_dir, footprint_mb, p50, rss_mb, write_result
from corpus import CLIPS

ENGINES = ["parakeet", "mlx-whisper", "server-2022", "server-2023"]
SERVER_PORTS = {"server-2022": 2022, "server-2023": 2023}
SNIPPETS_S = [0.4, 0.6, 0.8]
RUNS = 3
SKIPPED_EXIT = 3  # a child exits with this when the engine the session expected was skipped


class Parakeet:
    name = "parakeet-mlx parakeet-tdt-0.6b-v3"

    def __init__(self):
        import mlx.core as mx
        from parakeet_mlx import from_pretrained
        from parakeet_mlx.audio import get_logmel

        self.mx, self.get_logmel = mx, get_logmel
        self.model = from_pretrained("mlx-community/parakeet-tdt-0.6b-v3")

    def __call__(self, audio: np.ndarray) -> tuple[str, str | None]:
        mel = self.get_logmel(self.mx.array(audio), self.model.preprocessor_config)
        return self.model.generate(mel)[0].text.strip(), None

    def pid(self):
        return None


class MlxWhisper:
    name = "mlx-whisper whisper-large-v3-turbo"
    repo = "mlx-community/whisper-large-v3-turbo"

    def __init__(self):
        import mlx_whisper

        self.mlx_whisper = mlx_whisper
        self(np.zeros(SR, np.float32))

    def __call__(self, audio: np.ndarray) -> tuple[str, str | None]:
        out = self.mlx_whisper.transcribe(audio, path_or_hf_repo=self.repo, condition_on_previous_text=False)
        return out["text"].strip(), out.get("language")

    def pid(self):
        return None


class WhisperServer:
    def __init__(self, port: int):
        self.port = port
        self.client = httpx.Client(timeout=60)
        self.name = f"whisper.cpp server :{port} ({self.model_file()})"

    def model_file(self) -> str:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(self.pid())], capture_output=True, text=True).stdout
        match = re.search(r"--model (\S+)", cmd)
        return match.group(1).rsplit("/", 1)[-1] if match else "?"

    def pid(self) -> int | None:
        return listener_pid(self.port)

    def __call__(self, audio: np.ndarray) -> tuple[str, str | None]:
        buf = io.BytesIO()
        sf.write(buf, audio, SR, format="WAV", subtype="PCM_16")
        resp = self.client.post(
            f"http://127.0.0.1:{self.port}/v1/audio/transcriptions",
            files={"file": ("a.wav", buf.getvalue(), "audio/wav")},
            # whisper.cpp's server defaults to English, which turns Italian into an English translation.
            data={"response_format": "verbose_json", "temperature": "0", "language": "auto"},
        )
        resp.raise_for_status()
        body = resp.json()
        return body.get("text", "").strip(), body.get("language")


# Spellings that differ from the reference without being recognition errors.
EQUIVALENTS = {"pump kit": "pumpkit", "post hog": "posthog", "time zone": "timezone", "sub agent": "subagent",
               "sub agente": "subagente", "42": "forty two", "p r": "pr", "c i": "ci"}


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text.lower())
    text = re.sub(r"[^\w\s']", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for variant, canonical in EQUIVALENTS.items():
        text = re.sub(rf"\b{variant}\b", canonical, text)
    return text


def speech_onset(audio: np.ndarray) -> int:
    """First sample whose 10 ms frame rises well above the noise floor."""
    frames = audio[: len(audio) // 160 * 160].reshape(-1, 160)
    energy = np.sqrt((frames**2).mean(axis=1))
    floor = np.percentile(energy, 10)
    loud = np.nonzero(energy > max(floor * 6, 0.01))[0]
    return int(loud[0] * 160) if len(loud) else 0


def run_engine(engine: str, source: str) -> dict:
    from common import load_16k

    t0 = time.perf_counter()
    stt = {"parakeet": Parakeet, "mlx-whisper": MlxWhisper}.get(engine)
    stt = stt() if stt else WhisperServer(SERVER_PORTS[engine])
    load_ms = (time.perf_counter() - t0) * 1000
    folder = clips_dir(source) / "clips"
    clips = [c for c in CLIPS if (folder / f"{c.id}.wav").exists()]
    audio = {c.id: load_16k(folder / f"{c.id}.wav") for c in clips}
    stt(audio[clips[0].id])

    per_clip = {}
    for clip in clips:
        times, hyp = [], ("", None)
        for _ in range(RUNS):
            t = time.perf_counter()
            hyp = stt(audio[clip.id])
            times.append((time.perf_counter() - t) * 1000)
        per_clip[clip.id] = {
            "lang": clip.lang, "kind": clip.kind, "seconds": round(len(audio[clip.id]) / SR, 2),
            "ms_p50": round(p50(times), 1), "ref": clip.text, "hyp": hyp[0], "detected_lang": hyp[1],
        }

    def wer(ids):
        refs = [normalise(per_clip[i]["ref"]) for i in ids]
        hyps = [normalise(per_clip[i]["hyp"]) for i in ids]
        return round(jiwer.wer(refs, hyps) * 100, 1) if ids else None

    def lat(kind, lang=None):
        xs = [v["ms_p50"] for v in per_clip.values() if v["kind"] == kind and (lang is None or v["lang"] == lang)]
        return round(p50(xs), 1) if xs else None

    partials = {}
    for secs in SNIPPETS_S:
        times, samples = [], []
        for clip in [c for c in clips if c.kind == "short"]:
            start = speech_onset(audio[clip.id])
            snippet = audio[clip.id][start : start + int(secs * SR)]
            t = time.perf_counter()
            text, _ = stt(snippet)
            times.append((time.perf_counter() - t) * 1000)
            samples.append(f"{clip.id}: {text!r} <- {clip.text!r}")
        partials[f"{secs}s"] = {"ms_p50": round(p50(times), 1), "ms_max": round(max(times), 1), "samples": samples[:6]}

    pid = stt.pid()
    memory = {"rss": round(rss_mb(pid)), "footprint": footprint_mb(pid)}
    if pid is None:
        import mlx.core as mx

        # Footprint includes MLX's freed-buffer cache; active memory is what the model holds.
        memory |= {"mlx_active": round(mx.get_active_memory() / 2**20), "mlx_peak": round(mx.get_peak_memory() / 2**20),
                   "mlx_cache": round(mx.get_cache_memory() / 2**20)}
    return {
        "engine": stt.name, "source": source, "load_ms": round(load_ms),
        "latency_ms_p50": {"short_all": lat("short"), "long_all": lat("long"),
                           "short_en": lat("short", "en"), "short_it": lat("short", "it"),
                           "long_en": lat("long", "en"), "long_it": lat("long", "it")},
        "wer_pct": {"all": wer(list(per_clip)), "en": wer([i for i in per_clip if i.startswith("en")]),
                    "it": wer([i for i in per_clip if i.startswith("it")])},
        "partials": partials,
        "memory_mb": memory,
        "memory_note": "server process" if pid else "this process, model loaded",
        "clips": per_clip,
    }


def listener_pid(port: int) -> int | None:
    out = subprocess.run(["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def server_skip(engine: str, listener_pid: int | None, turbo_pid: int | None) -> str | None:
    """Why a server engine is not measured, or None when it is."""
    port = SERVER_PORTS[engine]
    if listener_pid is None:
        return f"nothing listening on :{port}"
    if engine != "server-2023":
        return None
    if turbo_pid is None:
        return f"pid {listener_pid} on :{port} was not started by the session (no --turbo-pid), so it is not the turbo row"
    if listener_pid != turbo_pid:
        return f"pid {listener_pid} on :{port} is not the turbo server the session started (pid {turbo_pid})"
    return None


def expected_but_skipped(engine: str, skipped: dict, turbo_pid: int | None) -> bool:
    """The session started a turbo server for this run, so leaving its row out is a failure, not a quiet skip."""
    return engine == "server-2023" and turbo_pid is not None and bool(skipped.get(engine))


def loud(text: str) -> None:
    bar = "!" * 72
    print(f"\n{bar}\n{text}\n{bar}\n", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["synthetic", "recordings"], default="synthetic")
    ap.add_argument("--engine", choices=ENGINES)
    ap.add_argument("--turbo-pid", type=int, help="pid of the whisper.cpp turbo server on :2023 to measure")
    args = ap.parse_args()
    skipped = {e: server_skip(e, listener_pid(SERVER_PORTS[e]), args.turbo_pid)
               for e in ([args.engine] if args.engine else ENGINES) if e in SERVER_PORTS}
    for engine, reason in skipped.items():
        if reason:
            loud(f"skip {engine}: {reason}")
    if args.engine:
        if skipped.get(args.engine):
            if expected_but_skipped(args.engine, skipped, args.turbo_pid):
                sys.exit(SKIPPED_EXIT)
            return
        result = run_engine(args.engine, args.source)
        path = write_result(f"stt-{args.engine}-{args.source}", result)
        print(json.dumps({k: v for k, v in result.items() if k != "clips"}, indent=2, ensure_ascii=False))
        print(f"-> {path}")
        return
    missing = []
    for engine in ENGINES:
        if skipped.get(engine):
            if expected_but_skipped(engine, skipped, args.turbo_pid):
                missing.append(f"{engine} (skipped)")
            continue
        cmd = [sys.executable, str(HERE / "bench_stt.py"), "--source", args.source, "--engine", engine]
        if engine == "server-2023" and args.turbo_pid is not None:
            cmd += ["--turbo-pid", str(args.turbo_pid)]
        code = subprocess.run(cmd).returncode
        if code:
            missing.append(f"{engine} ({'skipped' if code == SKIPPED_EXIT else f'exit {code}'})")
    if missing:
        sys.exit(f"STT engines that crashed or were skipped, so their rows are missing: {', '.join(missing)}")


if __name__ == "__main__":
    main()
