"""Memory with the whole audio stack loaded in one process, against the 4 GB audio-stack budget.

The `claude` processes already running, busy or idle, are recorded for reference only; bench_claude_idle.py measures idle ones.
"""

import argparse
import json
import statistics
import subprocess
import time

import numpy as np
import psutil

from common import SR, footprint_mb, rss_mb, write_result

BUDGET_MB = 4096


def snapshot(label: str, mx) -> dict:
    return {"stage": label, "rss": round(rss_mb()), "footprint": footprint_mb(),
            "mlx_active": round(mx.get_active_memory() / 2**20), "mlx_cache": round(mx.get_cache_memory() / 2**20)}


def claude_processes() -> list[dict]:
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        cmd = p.info["cmdline"] or []
        if cmd and (cmd[0] == "claude" or cmd[0].endswith("/claude")):
            out.append({"pid": p.info["pid"], "footprint": footprint_mb(p.info["pid"]), "rss": round(rss_mb(p.info["pid"]))})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stt", choices=["parakeet", "mlx-whisper"], default="parakeet")
    ap.add_argument("--cache-limit-mb", type=int, default=256)
    args = ap.parse_args()
    import mlx.core as mx

    # MLX keeps freed GPU buffers for reuse; the Hub would cap that cache, so the spike does too.
    mx.set_cache_limit(args.cache_limit_mb * 2**20)
    stages = [snapshot("python + mlx", mx)]
    speech = np.random.default_rng(0).normal(0, 0.05, 4 * SR).astype(np.float32)

    if args.stt == "parakeet":
        from bench_stt import Parakeet

        stt = Parakeet()
    else:
        from bench_stt import MlxWhisper

        stt = MlxWhisper()
    stt(speech)
    stages.append(snapshot(f"+ STT ({stt.name})", mx))

    from kokoro_mlx import KokoroTTS

    tts = KokoroTTS.from_pretrained("mlx-community/Kokoro-82M-bf16")
    tts.generate("I have opened the pull request on pumpkit and checks are running.", voice="am_liam")
    tts.generate("Ho aperto la pull request su pumpkit e i controlli sono partiti.", voice="im_nicola")
    stages.append(snapshot("+ TTS (kokoro-mlx, en + it voices)", mx))

    from openwakeword.model import Model
    from openwakeword.vad import VAD

    wake = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx", vad_threshold=0.5)
    pcm = (speech * 32767).astype(np.int16)
    for i in range(0, len(pcm) - 1280, 1280):
        wake.predict(pcm[i : i + 1280])
    vad = VAD()
    vad.predict(pcm[:512], 512)
    stages.append(snapshot("+ openWakeWord hey_jarvis + Silero VAD", mx))

    from bench_smart_turn import SmartTurn

    turn = SmartTurn("smart-turn-v3.2-cpu.onnx")
    turn(speech)
    stages.append(snapshot("+ Smart Turn v3.2 cpu", mx))

    from livekit import rtc

    apm = rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=True, high_pass_filter=True)
    frame = rtc.AudioFrame(pcm[:160].tobytes(), SR, 1, 160)
    apm.process_reverse_stream(frame)
    apm.process_stream(rtc.AudioFrame(pcm[:160].tobytes(), SR, 1, 160))
    stages.append(snapshot("+ WebRTC APM (livekit)", mx))

    for _ in range(3):
        stt(speech)
        tts.generate("One more sentence to keep the buffers warm, sir.", voice="am_liam")
    time.sleep(1)
    stages.append(snapshot("after warm reuse", mx))

    claudes = claude_processes()
    feet = [c["footprint"] for c in claudes if c["footprint"]]
    per_claude = statistics.median(feet) if feet else None
    audio = stages[-1]["footprint"]
    result = {
        "stt": args.stt, "mlx_cache_limit_mb": args.cache_limit_mb, "stages": stages,
        "mlx_peak_mb": round(mx.get_peak_memory() / 2**20),
        "claude_processes_seen": claudes, "claude_median_footprint_mb": per_claude,
        "audio_stack_footprint_mb": audio, "budget_mb": BUDGET_MB,
        "within_budget": audio is not None and audio <= BUDGET_MB,
        "audio_plus_three_claude_mb": round(audio + 3 * per_claude) if audio and per_claude else None,
        "machine_total_mb": round(psutil.virtual_memory().total / 2**20),
    }
    print(json.dumps({k: v for k, v in result.items() if k != "claude_processes_seen"}, indent=2))
    print(f"-> {write_result(f'memory-{args.stt}', result)}")


if __name__ == "__main__":
    main()
