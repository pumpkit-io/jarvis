"""Barge-in on the real input engines, from synthetic audio: no microphone, no speaker, no model.

Jarvis's reply (Kokoro, am_liam / im_nicola) is the echo: it goes to echo cancellation as the reference and, delayed
and quieter, into the "microphone" with the User's phrase (Kokoro in another voice) mixed in 2.5 s later. Frames go
through the real `InputPipeline` (WebRTC APM, openWakeWord, Silero VAD, parakeet-mlx) and the pure voice state
machine in audio time, with the reply pausing, rewinding and stopping as the player would. Writes numbers only.

    env -u VIRTUAL_ENV uv run python spikes/hub/bargein.py --out docs/notes/bargein.json
"""

import argparse
import json
import os
import platform
import statistics
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import soxr

from jarvis_hub.voice.bargein import Partial
from jarvis_hub.voice.engines import EngineChoice, OpenWakeWord, WebRtcEchoCancellation, load_engines
from jarvis_hub.voice.input import FRAME, SAMPLE_RATE, Engines, InputPipeline, Listening
from jarvis_hub.voice.player import Samples
from jarvis_hub.voice.state import BargeIn, Interrupt, Pause, Resume, Tick, Timing, Voice, WakeWord, barge_in_from, step
from jarvis_hub.voice.tts import KokoroSynthesizer

TIMING = Timing(follow_up_s=6.0, announce_follow_up_s=4.0, listen_s=6.0, chime_s=0.5)
TTS_RATE = 24_000
REF = TTS_RATE // 100
# Speaker to microphone: 40 ms late and at 40 % of the level played.
ECHO_DELAY_FRAMES, ECHO_GAIN = 4, 0.4
USER_AT_S = 2.5
REWIND_FRAMES = 30

REPLIES = {
    "en": "I've opened the pull request on pumpkit, sir, and the checks are running now. The deploy finished twelve "
    "minutes ago, and the error rate is flat compared with yesterday.",
    "it": "Ho aperto la pull request su pumpkit, signore, e i controlli sono in corso. Il deploy è terminato dodici "
    "minuti fa e il tasso di errori è stabile rispetto a ieri.",
}
USER_VOICES = {"en": "af_heart", "it": "if_sara"}

# (id, language, what the User says or a special source, the outcome expected)
CASES: list[tuple[str, str, str, str]] = [
    ("okay", "en", "Okay.", "resume"),
    ("mhm", "en", "Mhm.", "resume"),
    ("yeah-right", "en", "Yeah, right.", "resume"),
    ("wait-stop", "en", "Wait, stop.", "stop"),
    ("actually", "en", "Actually, open an issue instead.", "stop"),
    ("hey-jarvis", "en", "Hey Jarvis.", "stop"),
    ("si-certo", "it", "Sì, certo.", "resume"),
    ("va-bene", "it", "Va bene.", "resume"),
    ("aspetta", "it", "Aspetta, un attimo.", "stop"),
    ("anzi", "it", "Anzi, apri una issue.", "stop"),
    # Below the minimum speech: never even paused.
    ("cough-150ms", "en", "<noise 0.15>", "none"),
    # A door or a long cough: Silero does not take broadband noise for speech, so nothing pauses.
    ("noise-400ms", "en", "<noise 0.4>", "none"),
    ("echo-only", "en", "<none>", "none"),
    ("echo-only-it", "it", "<none>", "none"),
]


class TimedStt:
    def __init__(self, stt: Any) -> None:
        self._stt = stt
        self.ms: list[float] = []

    def transcribe(self, audio: Samples, *, after: float = 0.0) -> str:
        began = time.perf_counter()
        text = self._stt.transcribe(audio, after=after)
        self.ms.append((time.perf_counter() - began) * 1000)
        return text


def noise(seconds: float) -> Samples:
    """A burst of noise that dies away over `seconds`."""
    n = int(seconds * SAMPLE_RATE)
    burst = np.random.default_rng(1).normal(0, 1, n) * np.exp(-np.arange(n) / (seconds / 3 * SAMPLE_RATE))
    return (0.4 * burst / np.abs(burst).max()).astype(np.float32)


def user_audio(tts: KokoroSynthesizer, lang: str, said: str) -> Samples:
    if said == "<none>":
        return np.zeros(0, np.float32)
    if said.startswith("<noise "):
        return noise(float(said[7:-1]))
    audio = np.asarray(tts.synthesize(said, USER_VOICES[lang]), np.float32)
    return soxr.resample(audio, TTS_RATE, SAMPLE_RATE).astype(np.float32)


def run_case(engines: Engines, stt: TimedStt, reply24: Samples, user: Samples) -> dict[str, Any]:
    reply16 = soxr.resample(reply24, TTS_RATE, SAMPLE_RATE).astype(np.float32)
    frames = len(reply16) // FRAME
    pipeline = InputPipeline(engines, Listening(), record=lambda row: None)
    voice = replace(Voice(state="speaking"), voiced_at=0.0)
    since = barge_in_from(voice, TIMING)
    pipeline.barge_in(since)
    rng = np.random.default_rng(0)
    played: list[Samples] = [np.zeros(FRAME, np.float32)] * ECHO_DELAY_FRAMES
    user_start = round(USER_AT_S * 100) * FRAME
    loud = np.flatnonzero(np.abs(user) > 0.02)
    onset = (user_start + loud[0]) / SAMPLE_RATE if len(loud) else None
    k, paused, n = 0, False, 0
    stt.ms.clear()
    out: dict[str, Any] = {"onset_s": onset, "barge_ins": 0, "wake_words": 0, "pauses": [], "decisions": []}
    while k < frames and n < frames + 300:
        t = (n + 1) / 100
        block = np.zeros(FRAME, np.float32) if paused else reply16[k * FRAME : (k + 1) * FRAME]
        reference = np.zeros(REF, np.float32) if paused else reply24[k * REF : (k + 1) * REF]
        engines.echo.reference(reference, TTS_RATE)
        played.append(block)
        mic = ECHO_GAIN * played.pop(0) + rng.normal(0, 0.002, FRAME).astype(np.float32)
        u = user[max(0, n * FRAME - user_start) : max(0, (n + 1) * FRAME - user_start)] if n * FRAME >= user_start else []
        mic[: len(u)] += u
        events = pipeline.push((np.clip(mic, -1, 1) * 32767).astype(np.int16), t)
        for event in [*events, Tick(t)]:
            if isinstance(event, BargeIn):
                out["barge_ins"] += 1
            if isinstance(event, WakeWord):
                out["wake_words"] += 1
            if isinstance(event, Partial) and os.environ.get("DEBUG"):
                print("   partial", repr(event.text), round(event.speech_s, 2), round(event.silent_s, 2))
            voice, actions = step(voice, event, TIMING)
            if barge_in_from(voice, TIMING) != since:
                since = barge_in_from(voice, TIMING)
                pipeline.barge_in(since)
            for action in actions:
                match action:
                    case Pause():
                        paused = True
                        out["pauses"].append(round(t, 2))
                    case Resume():
                        paused, k = False, max(0, k - REWIND_FRAMES)
                        out["decisions"].append(("resume", round(t, 2)))
                    case Interrupt():
                        out["decisions"].append(("stop", round(t, 2)))
                    case _:
                        pass
        if out["decisions"] and out["decisions"][-1][0] == "stop":
            break
        if not paused:
            k += 1
        n += 1
    out["partial_stt_ms"] = [round(ms, 1) for ms in stt.ms]
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    tts = KokoroSynthesizer()
    tts.load()
    loaded = load_engines(EngineChoice(aec="webrtc"))
    stt = TimedStt(loaded.stt)
    replies = {lang: np.asarray(tts.synthesize(text, "am_liam" if lang == "en" else "im_nicola"), np.float32) for lang, text in REPLIES.items()}
    rows: list[dict[str, Any]] = []
    for case, lang, said, expected in CASES:
        # A fresh echo canceller and Wake word model per case, so nothing one case leaves behind reaches the next.
        engines = replace(loaded, echo=WebRtcEchoCancellation(), wake=OpenWakeWord(), stt=stt)
        result = run_case(engines, stt, replies[lang], user_audio(tts, lang, said))
        outcome = result["decisions"][0][0] if result["decisions"] else "none"
        decided_ms = None
        if result["decisions"] and result["onset_s"] is not None:
            decided_ms = round((result["decisions"][0][1] - result["onset_s"]) * 1000)
        rows.append(
            {
                "case": case,
                "lang": lang,
                "expected": expected,
                "outcome": outcome,
                "ok": outcome == expected,
                # Audio time from the User's first sound to the decision; the partials' STT time comes on top.
                "decided_ms": decided_ms,
                "partial_stt_ms": result["partial_stt_ms"],
                "barge_ins": result["barge_ins"],
                "wake_words": result["wake_words"],
                "pauses_s": result["pauses"],
                "decisions": [d for d, _ in result["decisions"]],
            }
        )
        print(rows[-1])
    # Frames queue behind a partial's STT and catch up after it, so only the deciding partial's STT time adds up.
    resumed = [r["decided_ms"] + r["partial_stt_ms"][-1] for r in rows if r["decided_ms"] is not None and r["outcome"] == "resume"]
    stopped = [r["decided_ms"] + r["partial_stt_ms"][-1] for r in rows if r["decided_ms"] is not None and r["outcome"] == "stop"]
    summary = {
        "date": date.today().isoformat(),
        "machine": platform.machine(),
        "cases": len(rows),
        "as_expected": sum(r["ok"] for r in rows),
        "resume_ms_max": max(resumed) if resumed else None,
        "stop_ms_max": max(stopped) if stopped else None,
        "partial_stt_ms_median": statistics.median(ms for r in rows for ms in r["partial_stt_ms"][1:] or r["partial_stt_ms"]),
        "rows": rows,
    }
    args.out.write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
