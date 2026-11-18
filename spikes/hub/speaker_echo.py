"""How much of Jarvis's own voice survives echo cancellation on the built-in speakers, and what the pipeline makes of it.

`record` plays Jarvis replies through the MacBook speakers in a silent room while recording the MacBook microphone
and every block sent to the speakers, with their times. `analyse` replays that recording offline through the WebRTC
echo canceller at several stream delays, then through the real VAD, Wake word, STT and Barge-in detection, and
counts what a silent User would have been heard saying.

    uv run python spikes/hub/speaker_echo.py record
    uv run python spikes/hub/speaker_echo.py analyse [--delays none,0,50,...]
"""

import argparse
from contextlib import closing
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from jarvis_hub.voice.bargein import Partial, decide
from jarvis_hub.voice.engines import EngineChoice, WebRtcEchoCancellation, load_engines
from jarvis_hub.voice.input import FRAME, SAMPLE_RATE, Engines, InputPipeline, Listening
from jarvis_hub.voice.state import BargeIn, SpeechStarted, WakeWord
from jarvis_hub.voice.tts import KokoroSynthesizer
from jarvis_hub.voice.transcription import TranscriptionService

OUT = Path(".jarvis/echo")
VOICE = "am_liam"
GAP_S = 2.0
REPLIES = [
    "I didn't catch your meaning, sir. Did you mean something about task two, or a different PR number?",
    "Understood, sir.",
    "Of nine open PRs, only the docs PR 543 is failing, sir. Its changelog check fails because it has no "
    "changelog entry and no no-changelog label.",
    "Take your time, sir. I'll hold until you tell me what to do.",
    "A programmer's partner asks him to pop to the shop for a loaf of bread, and if they have eggs, to buy six. "
    "He returns with six loaves. They had eggs, he says.",
    "I haven't started anything, sir. I only asked whether to add the entry, and PR 543 is untouched.",
    "Yes, sir. It's Amazon's managed service for accessing foundation models, including Claude, through a single "
    "API. It handles hosting, scaling and access control, so you don't run the models yourself.",
    "Waiting, sir. Nothing has been changed on PR 543.",
    "Seven repos, sir. Pumpkit has eight open PRs and jarvis has twelve, and nearly all are passing CI.",
    "Stopped, sir.",
]


def device(sd: Any, name: str, kind: str) -> int:
    for index, info in enumerate(sd.query_devices()):
        if name in info["name"] and info[f"max_{kind}_channels"] > 0:
            return index
    raise SystemExit(f"No {kind} device named {name!r}")


def record() -> None:
    import sounddevice as sd  # pyright: ignore[reportMissingTypeStubs]

    tts = KokoroSynthesizer()
    tts.load()
    rate = tts.sample_rate
    pieces: list[np.ndarray] = [np.zeros(int(rate * 1.0), np.float32)]
    spans: list[tuple[float, float]] = []
    at = 1.0
    for text in REPLIES:
        samples = tts.synthesize(text, VOICE).astype(np.float32)
        spans.append((at, at + len(samples) / rate))
        at += len(samples) / rate + GAP_S
        pieces += [samples, np.zeros(int(rate * GAP_S), np.float32)]
    program = np.concatenate(pieces)
    print(f"Playing {len(program) / rate:.1f} s through the MacBook speakers; stay silent.", flush=True)

    captured: list[tuple[float, np.ndarray]] = []
    played: list[tuple[float, np.ndarray]] = []
    position = [0]
    done = threading.Event()

    def out(outdata: Any, frames: int, when: Any, status: Any) -> None:
        start = position[0]
        block = program[start : start + frames]
        outdata[:, 0] = 0
        outdata[: len(block), 0] = block
        played.append((time.perf_counter(), outdata[:, 0].copy()))
        position[0] += frames
        if position[0] >= len(program) + rate:
            done.set()

    def inp(indata: Any, frames: int, when: Any, status: Any) -> None:
        captured.append((time.perf_counter(), indata[:, 0].copy()))

    speaker, microphone = device(sd, "MacBook Pro Speakers", "output"), device(sd, "MacBook Pro Microphone", "input")
    with sd.InputStream(device=microphone, samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME,
                        latency="low", callback=inp) as instream, \
         sd.OutputStream(device=speaker, samplerate=rate, channels=1, dtype="float32", latency=0.1,
                         callback=out) as outstream:
        started = time.perf_counter()
        done.wait(len(program) / rate + 10)
        latencies = {"input_s": instream.latency, "output_s": outstream.latency}
    OUT.mkdir(parents=True, exist_ok=True)
    np.savez(OUT / "recording.npz",
             cap_t=np.array([t for t, _ in captured]), cap=np.stack([f for _, f in captured]),
             ref_t=np.array([t for t, _ in played]), ref=np.concatenate([b for _, b in played]),
             ref_n=np.array([len(b) for _, b in played]), rate=rate)
    (OUT / "recording.json").write_text(json.dumps({
        "started": started, "spans": spans, "latencies": latencies, "voice": VOICE, "gap_s": GAP_S,
    }, indent=1))
    print(f"Saved {len(captured)} capture frames, {len(played)} reference blocks; latencies {latencies}.")


class DelayedAec(WebRtcEchoCancellation):
    def __init__(self, delay_ms: int | None) -> None:
        super().__init__()
        self._delay = delay_ms

    def process(self, frame: Any) -> Any:
        if self._delay is not None:
            self._apm.set_stream_delay_ms(self._delay)
        return super().process(frame)


def cancel(data: Any, delay_ms: int | None) -> np.ndarray:
    aec = DelayedAec(delay_ms)
    rate = int(data["rate"])
    offsets = np.concatenate([[0], np.cumsum(data["ref_n"])])
    blocks = [(t, data["ref"][offsets[i] : offsets[i + 1]]) for i, t in enumerate(data["ref_t"])]
    out: list[np.ndarray] = []
    b = 0
    for t, frame in zip(data["cap_t"], data["cap"]):
        while b < len(blocks) and blocks[b][0] <= t:
            aec.reference(blocks[b][1], rate)
            b += 1
        out.append(aec.process(frame))
    return np.stack(out)


def rms_db(x: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(x.astype(np.float64) ** 2)) + 1e-9))


def heard(engines: Engines, frames: np.ndarray, times: np.ndarray, spans: list[tuple[float, float]]) -> dict[str, Any]:
    """What a silent User's microphone would raise: Barge-ins over the voice, speech right after it, and their words."""
    def where(at: float) -> str:
        for start, end in spans:
            if start <= at <= end:
                return "over_voice"
            if end < at <= end + 1.0:
                return "tail"
        return "gap"

    pipeline = InputPipeline(engines, Listening(), record=lambda row: None)
    pipeline.barge_in(float(times[0]))
    events: list[Any] = []
    # As the state machine does once a Barge-in is decided: watch again from a guard past the decision.
    rearm_at = float("inf")
    vad_buf = np.zeros(0, np.int16)
    tail_speech_s = 0.0
    for t, frame in zip(times, frames):
        new = pipeline.push_clean(frame, float(t))
        events += [(float(t), e) for e in new]
        if any(isinstance(e, BargeIn) for e in new):
            rearm_at = float(t) + 1.3
        if t >= rearm_at:
            pipeline.barge_in(float(t) + 0.25)
            rearm_at = float("inf")
        vad_buf = np.concatenate([vad_buf, frame])
        while len(vad_buf) >= engines.vad.chunk:
            chunk, vad_buf = vad_buf[: engines.vad.chunk], vad_buf[engines.vad.chunk :]
            if engines.vad.probability(chunk) >= 0.5 and where(float(t)) == "tail":
                tail_speech_s += engines.vad.chunk / SAMPLE_RATE

    barges: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for _, event in events:
        if isinstance(event, BargeIn):
            current = {"onset": event.onset, "where": where(event.onset), "partials": []}
            barges.append(current)
        elif isinstance(event, Partial) and current is not None:
            current["partials"].append(event)
    for barge in barges:
        partials: list[Partial] = barge.pop("partials")
        barge["text"] = partials[-1].text if partials else ""
        barge["decision"] = decide(partials, final=True) if partials else "resume"
    onsets = [e.at for _, e in events if isinstance(e, SpeechStarted)]
    return {
        "barge_ins": barges,
        "wake_hits": sum(isinstance(e, WakeWord) for _, e in events),
        "speech_onsets": len(onsets),
        "tail_speech_s": round(tail_speech_s, 2),
    }


def analyse(delays: list[int | None]) -> None:
    data = np.load(OUT / "recording.npz")
    meta = json.loads((OUT / "recording.json").read_text())
    started = meta["started"]
    spans = [(started + a + 0.0, started + b) for a, b in meta["spans"]]
    with closing(TranscriptionService()) as transcription:
        engines = load_engines(EngineChoice(aec="off"), transcription=transcription)
        times = data["cap_t"]
        voiced = np.zeros(len(times), bool)
        for start, end in spans:
            voiced |= (times >= start) & (times <= end)
        raw = data["cap"]
        results: dict[str, Any] = {"latencies": meta["latencies"], "raw_voice_db": rms_db(raw[voiced]),
                                   "raw_silence_db": rms_db(raw[~voiced]), "settings": {}}
        for delay in delays:
            clean = cancel(data, delay)
            found = heard(engines, clean, times, spans)
            stops = [b for b in found["barge_ins"] if b["decision"] == "stop"]
            results["settings"][str(delay)] = {
                "voice_db": rms_db(clean[voiced]), "silence_db": rms_db(clean[~voiced]),
                "erle_db": rms_db(raw[voiced]) - rms_db(clean[voiced]),
                "barge_ins": len(found["barge_ins"]), "stops": len(stops),
                "worded": [(b["where"], b["text"]) for b in found["barge_ins"] if b["text"]],
                "wake_hits": found["wake_hits"], "tail_speech_s": found["tail_speech_s"],
            }
            row = results["settings"][str(delay)]
            print(f"delay {delay}: ERLE {row['erle_db']:.1f} dB, barge-ins {row['barge_ins']}, stops {row['stops']}, "
                  f"wake {row['wake_hits']}, tail speech {row['tail_speech_s']} s, words {row['worded']}", flush=True)
        (OUT / "analysis.json").write_text(json.dumps(results, indent=1, default=str))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=["record", "analyse"])
    parser.add_argument("--delays", default="none,0,40,80,120,160,200,250,300")
    args = parser.parse_args()
    if args.step == "record":
        record()
    else:
        analyse([None if d == "none" else int(d) for d in args.delays.split(",")])


if __name__ == "__main__":
    sys.exit(main())
