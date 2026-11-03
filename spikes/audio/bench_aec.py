"""Echo cancellation: how often VAD fires on Jarvis's own voice, with and without AEC.

    uv run bench_aec.py simulate                  # digital echo, proves the pipeline only
    uv run bench_aec.py analyse <speaker run dir> # the session's built-in speaker run

A run dir holds `reference.wav` (what played) and `mic.wav` (raw mic, sample-aligned
with the reference by a duplex stream), optionally `vpio.wav` (macOS Voice Processing
I/O output from vpio_playrec.swift) and `segments.json` marking where the User spoke.
"""

import argparse
import json
from pathlib import Path

import numpy as np
from livekit import rtc
from openwakeword.vad import VAD

from common import DATA, SR, load_16k, save_16k, write_result

FRAME = SR // 100  # the APM only accepts exactly 10 ms frames
VAD_FRAME = 512
SPEECH_P = 0.5
TRIGGER_MS = 200  # JARVIS_BARGEIN_MIN_SPEECH_MS
DELAYS_MS = [0, 20, 40, 60, 80, 120]


def to_i16(x: np.ndarray) -> np.ndarray:
    return (np.clip(x, -1, 1) * 32767).astype(np.int16)


def webrtc_aec(mic: np.ndarray, ref: np.ndarray, delay_ms: int) -> np.ndarray:
    apm = rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=True, high_pass_filter=True,
                                    auto_gain_control=False)
    apm.set_stream_delay_ms(delay_ms)
    mic16, ref16 = to_i16(mic), to_i16(ref)
    out = np.zeros_like(mic16)
    for i in range(0, min(len(mic16), len(ref16)) - FRAME + 1, FRAME):
        rframe = rtc.AudioFrame(ref16[i : i + FRAME].tobytes(), SR, 1, FRAME)
        apm.process_reverse_stream(rframe)
        mframe = rtc.AudioFrame(mic16[i : i + FRAME].tobytes(), SR, 1, FRAME)
        apm.process_stream(mframe)
        out[i : i + FRAME] = np.frombuffer(bytes(mframe.data), np.int16)
    return out.astype(np.float32) / 32767


def vad_probs(audio: np.ndarray) -> np.ndarray:
    vad = VAD()
    pcm = to_i16(audio)
    return np.array([vad.predict(pcm[i : i + VAD_FRAME], VAD_FRAME) for i in range(0, len(pcm) - VAD_FRAME + 1, VAD_FRAME)])


def triggers(probs: np.ndarray, start_s: float = 0, end_s: float | None = None) -> dict:
    """Count Barge-in triggers: runs of speech frames at least TRIGGER_MS long."""
    fs = VAD_FRAME / SR
    a, b = int(start_s / fs), int(end_s / fs) if end_s else len(probs)
    speech = probs[a:b] > SPEECH_P
    need = int(np.ceil(TRIGGER_MS / 1000 / fs))
    count, run = 0, 0
    for s in speech:
        run = run + 1 if s else 0
        if run == need:
            count += 1
    minutes = (b - a) * fs / 60
    return {"triggers": count, "per_minute": round(count / minutes, 2) if minutes else None,
            "speech_frame_pct": round(float(speech.mean() * 100), 1) if len(speech) else None}


def analyse(run: Path) -> dict:
    ref = load_16k(run / "reference.wav")
    mic = load_16k(run / "mic.wav")
    segments = json.loads((run / "segments.json").read_text()) if (run / "segments.json").exists() else {}
    user = segments.get("user_speech", [])
    jarvis_only = [(0.0, user[0][0])] if user else [(0.0, len(mic) / SR)]

    def score(audio: np.ndarray, spans: list = user) -> dict:
        probs = vad_probs(audio)
        out = {"jarvis_only": triggers(probs, *jarvis_only[0])}
        if spans:
            hits = [triggers(probs, s, e)["triggers"] > 0 for s, e in spans]
            out["user_barge_ins_detected"] = f"{sum(hits)}/{len(hits)}"
        return out

    result = {"run": str(run), "seconds": round(len(mic) / SR, 1), "raw_mic": score(mic), "webrtc": {}}
    for delay in DELAYS_MS:
        result["webrtc"][f"delay={delay}ms"] = score(webrtc_aec(mic, ref, delay))
    if (run / "vpio.wav").exists():
        result["macos_vpio"] = score(load_16k(run / "vpio.wav"), segments.get("user_speech_vpio", user))
    return result


def simulate() -> Path:
    """Build a run dir with digital echo: Kokoro speech through a toy room, plus a User clip."""
    from kokoro_mlx import KokoroTTS

    from common import SYNTHETIC, resample
    from corpus import JARVIS_LINES

    tts = KokoroTTS.from_pretrained("mlx-community/Kokoro-82M-bf16")
    parts = []
    for lang, line in JARVIS_LINES:
        audio = tts.generate(line, voice="am_liam" if lang == "en" else "im_nicola").audio
        parts += [resample(np.asarray(audio, np.float32), tts.SAMPLE_RATE, SR), np.zeros(SR // 2, np.float32)]
    ref = np.concatenate(parts)
    ref = np.concatenate([ref, ref])
    rng = np.random.default_rng(1)
    room = np.zeros(int(0.25 * SR), np.float32)
    room[int(0.035 * SR)] = 0.6
    taps = rng.integers(int(0.04 * SR), len(room), 40)
    room[taps] += rng.normal(0, 0.12, 40) * np.exp(-(taps / SR) / 0.08)
    echo = np.convolve(ref, room)[: len(ref)]
    mic = echo + rng.normal(0, 0.003, len(ref)).astype(np.float32)
    user_spans = []
    half = len(ref) // 2
    for i, clip_id in enumerate(["en06", "it05", "en03"]):
        clip = load_16k(SYNTHETIC / "clips" / f"{clip_id}.wav") * 0.7
        at = half + int((i + 0.3) * (len(ref) - half) / 3)
        mic[at : at + len(clip)] += clip[: len(mic) - at]
        user_spans.append([round(at / SR, 2), round((at + len(clip)) / SR, 2)])
    run = DATA / "synthetic" / "speaker-run"
    save_16k(run / "reference.wav", ref)
    save_16k(run / "mic.wav", mic.astype(np.float32))
    (run / "segments.json").write_text(json.dumps({"user_speech": user_spans, "note": "digital echo, 35 ms delay"}))
    return run


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("simulate")
    a = sub.add_parser("analyse")
    a.add_argument("run", type=Path)
    args = ap.parse_args()
    run = simulate() if args.cmd == "simulate" else args.run
    result = analyse(run)
    print(json.dumps(result, indent=2))
    print(f"-> {write_result('aec-' + ('synthetic' if args.cmd == 'simulate' else run.name), result)}")


if __name__ == "__main__":
    main()
