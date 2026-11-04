"""Drives voice mode's real input pipeline end to end from synthetic WAV files, with no microphone and no model.

Frames are pushed in real time through `Capture` (WebRTC APM, openWakeWord, Silero VAD, Smart Turn v3.2,
parakeet-mlx) into `VoiceMode`; each turn the Hub would get is answered with a canned spoken reply through the
real Kokoro voice and a silent player. Writes numbers only (no transcripts) to `--out`.

    env -u VIRTUAL_ENV uv run python spikes/hub/voice_input.py \\
        --data ../voice-hub-t5/spikes/audio/data/synthetic --out docs/notes/voice-input.json
"""

import argparse
import asyncio
import json
import os
import platform
import queue
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date
from importlib.metadata import version
from pathlib import Path
from collections.abc import AsyncIterator
from typing import Any

import mlx.core as mx
import numpy as np
import soundfile as sf
import soxr

from jarvis_hub.voice.engines import EngineChoice, load_engines
from jarvis_hub.voice.input import FRAME, SAMPLE_RATE, Capture, Listening, Push
from jarvis_hub.voice.player import Clip
from jarvis_hub.voice.state import InputEvent, Timing, WakeWord
from jarvis_hub.voice.tts import KokoroSynthesizer
from jarvis_hub.voice.voice_mode import VoiceMode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "audio"))
from corpus import CLIPS, WAKE_PHRASINGS  # noqa: E402  # pyright: ignore

# Shorter windows than the defaults, so the run spends less time waiting to idle; the logic is the same.
TIMING = Timing(follow_up_s=3.0, announce_follow_up_s=2.0, listen_s=3.0, chime_s=0.5)
REPLY = "Very good, sir."


class Clock:
    def now(self) -> float:
        return time.perf_counter()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class SilentPlayer:
    """Plays at once and keeps nothing; the run is about input, and nothing reaches a speaker to echo."""

    def play(self, clip: Clip, *, starts_reply: bool = False) -> None:
        pass

    def pause(self) -> None:
        pass

    def resume(self, rewind_ms: int = 300) -> None:
        pass

    def stop(self) -> None:
        pass

    def played(self) -> str:
        return ""

    def pending(self) -> float:
        return 0.0

    def close(self) -> None:
        pass


class RealTimeSource:
    """Pushes queued audio as 10 ms frames at the pace of a real microphone, and silence when nothing is queued."""

    def __init__(self) -> None:
        self._segments: queue.SimpleQueue[tuple[np.ndarray, threading.Event, list[float]]] = queue.SimpleQueue()
        self._stop = threading.Event()

    def open(self, push: Push) -> None:
        threading.Thread(target=self._run, args=(push,), daemon=True).start()

    def close(self) -> None:
        self._stop.set()

    def play(self, audio: np.ndarray) -> tuple[threading.Event, list[float]]:
        """Queues audio; the list gets the wall time its first sample was pushed, the event is set once all were."""
        done, started = threading.Event(), list[float]()
        self._segments.put((audio, done, started))
        return done, started

    def _run(self, push: Push) -> None:
        silence = np.zeros(FRAME, np.int16)
        current: tuple[np.ndarray, threading.Event, list[float]] | None = None
        pos = 0
        due = time.perf_counter()
        while not self._stop.is_set():
            if current is None and not self._segments.empty():
                current, pos = self._segments.get(), 0
            if current is None:
                frame = silence
            else:
                audio, done, started = current
                if pos == 0:
                    started.append(due)
                frame = audio[pos : pos + FRAME]
                frame = np.pad(frame, (0, FRAME - len(frame)))
                pos += FRAME
                if pos >= len(audio):
                    done.set()
                    current = None
            due += FRAME / SAMPLE_RATE
            delay = due - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            push(frame, due)


def load(path: Path) -> np.ndarray:
    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if rate != SAMPLE_RATE:
        audio = soxr.resample(audio, rate, SAMPLE_RATE)
    return audio.astype(np.float32)


def speech_end_s(audio: np.ndarray) -> float:
    """The end of the last 10 ms frame above -40 dBFS, as the spikes measure it."""
    frames = audio[: len(audio) // FRAME * FRAME].reshape(-1, FRAME)
    loud = np.nonzero(np.sqrt((frames**2).mean(axis=1)) > 0.01)[0]
    return (loud[-1] + 1) * FRAME / SAMPLE_RATE if len(loud) else len(audio) / SAMPLE_RATE


def pcm(audio: np.ndarray) -> np.ndarray:
    return (np.clip(audio, -1, 1) * 32767).astype(np.int16)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(round(seconds * SAMPLE_RATE), np.float32)


def say(text: str, voice: str, folder: Path) -> np.ndarray:
    path = folder / f"{abs(hash((text, voice)))}.aiff"
    subprocess.run(["say", "-v", voice, "-o", str(path), text], check=True)
    return load(path)


def words(text: str) -> list[str]:
    return re.findall(r"[\w']+", text.lower().replace("pump kit", "pumpkit"))


def footprint_mb() -> float | None:
    out = subprocess.run(["footprint", "-p", str(os.getpid())], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "Footprint:" in line and "phys_footprint" not in line:
            value, unit = line.split("Footprint:")[1].split()[:2]
            return float(value) * {"KB": 1 / 1024, "MB": 1, "GB": 1024}[unit]
    return None


def summary(xs: list[float]) -> dict[str, float | int | None]:
    if not xs:
        return {"n": 0, "p50": None, "p90": None, "max": None}
    ordered = sorted(xs)
    return {
        "n": len(xs),
        "p50": round(statistics.median(ordered), 1),
        "p90": round(ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))], 1),
        "max": round(ordered[-1], 1),
    }


class Rows:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record(self, entry: dict[str, Any]) -> None:
        self.rows.append(entry)


class TimedInput:
    """The capture's events, noting when each Wake word reached the event loop."""

    def __init__(self, capture: Capture) -> None:
        self._capture = capture
        self.wakes: list[float] = []

    def capture(self, on: bool) -> None:
        self._capture.capture(on)

    def barge_in(self, since: float | None) -> None:
        self._capture.barge_in(since)

    async def events(self) -> AsyncIterator[InputEvent]:
        async for event in self._capture.events():
            if isinstance(event, WakeWord):
                self.wakes.append(time.perf_counter())
            yield event


class Run:
    def __init__(self, voice: VoiceMode, source: RealTimeSource, rows: list[dict[str, Any]]) -> None:
        self.voice = voice
        self.source = source
        self.rows = rows
        self.turns: list[tuple[float, str]] = []
        self.wakes: list[float] = []

    async def consume(self) -> None:
        turn = 0
        async for line in self.voice.lines(self._interrupt):
            self.turns.append((time.perf_counter(), line.text))
            turn += 1
            self.voice.speech.begin("en", turn=turn, heard_at=line.arrived)
            self.voice.speech.feed(REPLY)
            self.voice.speech.end()

    async def _interrupt(self) -> bool:
        self.voice.speech.stop()
        return False

    async def play(self, audio: np.ndarray) -> float:
        done, started = self.source.play(pcm(audio))
        await asyncio.to_thread(done.wait)
        return started[0]

    async def until_idle(self, timeout: float = 15.0) -> bool:
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            if self.voice.state == "idle":
                return True
            await asyncio.sleep(0.05)
        return False

    def transitions_since(self, i: int) -> list[str]:
        return [r["to"] for r in self.rows[i:] if r["kind"] == "voice"]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True, help="the synthetic folder of the #5 spikes (clips/, wake/)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--aec", choices=["webrtc", "off"], default="webrtc")
    ap.add_argument("--transcripts", type=Path, help="also write what each turn heard here, for diagnosis; keep it local")
    args = ap.parse_args()

    metrics = Rows()
    rows = metrics.rows
    started = time.perf_counter()
    footprint_start = footprint_mb()
    source = RealTimeSource()
    loaded = threading.Event()

    def engines() -> Any:
        e = load_engines(EngineChoice(aec=args.aec))
        loaded.set()
        return e

    capture = Capture(engines, Listening(), metrics.record, print, source=source)
    timed = TimedInput(capture)
    voice = VoiceMode(
        audio=timed, tts=KokoroSynthesizer(), player=SilentPlayer(), clock=Clock(), timing=TIMING,
        metrics=metrics, notice=print,
    )
    voice.start()
    capture.start()
    await asyncio.to_thread(capture.ready.wait)
    await voice.idle()
    ready_s = time.perf_counter() - started
    run = Run(voice, source, rows)
    consumer = asyncio.create_task(run.consume())
    results: dict[str, Any] = {"date": date.today().isoformat(), "ready_s": round(ready_s, 2)}

    wake_takes = sorted((args.data / "wake").glob("*.wav"))
    clips = {c.id: c for c in CLIPS if (args.data / "clips" / f"{c.id}.wav").exists()}
    clip_audio = {k: load(args.data / "clips" / f"{k}.wav") for k in clips}

    # 1. A wake phrase on its own: detected, chimed, and the phrase alone never becomes a turn.
    per_phrasing: dict[str, list[bool]] = {k: [] for k in WAKE_PHRASINGS}
    wake_ms: list[float] = []
    stray_turns = 0
    for take in wake_takes:
        audio = load(take)
        mark, n_turns, n_wakes = len(rows), len(run.turns), len(timed.wakes)
        t0 = await run.play(np.concatenate([audio, silence(0.5)]))
        await asyncio.sleep(1.5)
        woke = "listening" in run.transitions_since(mark)
        per_phrasing[take.stem.split("_")[0]].append(woke)
        if len(timed.wakes) > n_wakes:
            wake_ms.append((timed.wakes[n_wakes] - (t0 + speech_end_s(audio))) * 1000)
        stray_turns += len(run.turns) - n_turns
        await run.until_idle()
    results["wake_alone"] = {
        "recall_at_0.5": {WAKE_PHRASINGS[k]: f"{sum(v)}/{len(v)}" for k, v in per_phrasing.items()},
        "wake_after_phrase_end_ms": summary(wake_ms),
        "wake_phrase_sent_as_a_turn": stray_turns,
    }

    # 2. "Hey Jarvis, <request>" in one breath (150 ms gap), and 3. with a 1 s pause for the chime.
    hey = [load(t) for t in wake_takes if t.stem.startswith("hey_")]
    for name, gap in [("one_breath", 0.15), ("pause_for_chime", 1.0)]:
        eot, right, exact, missed, stt, smart, incomplete = [], 0, 0, 0, [], [], 0
        for i, (cid, clip) in enumerate(clips.items()):
            wake_audio = hey[i % len(hey)]
            head = wake_audio[: int(speech_end_s(wake_audio) * SAMPLE_RATE) + FRAME]
            audio = np.concatenate([silence(0.3), head, silence(gap), clip_audio[cid]])
            end = speech_end_s(audio)
            mark, n_turns = len(rows), len(run.turns)
            t0 = await run.play(np.concatenate([audio, silence(2.5)]))
            await asyncio.sleep(0.5)
            got = run.turns[n_turns:]
            if not got:
                missed += 1
            else:
                wall, text = got[0]
                eot.append((wall - (t0 + end)) * 1000)
                expected, heard = words(clip.text), words(text)
                exact += heard == expected
                right += sum(a == b for a, b in zip(heard, expected)) >= len(expected) - 1
                turn_rows = [r for r in rows[mark:] if r["kind"] == "turn" and r["words"]]
                if turn_rows:
                    stt.append(turn_rows[-1]["stt_ms"])
                    smart.append(turn_rows[-1]["smart_turn_ms"])
                    incomplete += any(v < Listening().smart_turn_threshold for v in turn_rows[-1]["smart_turn"])
            await run.until_idle()
        results[name] = {
            "turns": len(clips),
            "missed": missed,
            "transcript_exact": exact,
            "transcript_within_one_word": right,
            "speech_end_to_turn_ms": summary(eot),
            "stt_ms": summary(stt),
            "smart_turn_ms": summary(smart),
            "turns_judged_incomplete": incomplete,
        }

    # 4. The Follow-up window: a turn without the Wake word inside it, then silence to idle.
    follow_ok = 0
    for cid in list(clips)[:6]:
        n_turns = len(run.turns)
        await run.play(np.concatenate([hey[0][: int(speech_end_s(hey[0]) * SAMPLE_RATE)], silence(0.15), clip_audio["en01"], silence(1.5)]))
        await asyncio.sleep(0.5)
        await run.play(np.concatenate([clip_audio[cid], silence(1.5)]))
        await asyncio.sleep(0.3)
        follow_ok += len(run.turns) - n_turns == 2
        await run.until_idle()
    results["follow_up"] = {"window_s": TIMING.follow_up_s, "follow_up_turns_heard": f"{follow_ok}/6"}

    # 5. Stop phrases said on their own, and 6. requests with no Wake word while idle.
    with tempfile.TemporaryDirectory() as tmp:
        stops = [say("That's all.", "Samantha", Path(tmp)), say("Grazie Jarvis.", "Alice", Path(tmp))]
    stop_ok = 0
    for stop in stops:
        n_turns = len(run.turns)
        await run.play(np.concatenate([hey[1][: int(speech_end_s(hey[1]) * SAMPLE_RATE)], silence(0.15), clip_audio["en09"], silence(1.5)]))
        await asyncio.sleep(0.5)
        await run.play(np.concatenate([stop, silence(0.5)]))
        # Idle well inside the 3 s window, and with no turn sent, means the stop phrase did it.
        idled = await run.until_idle(timeout=1.5)
        stop_ok += idled and len(run.turns) - n_turns == 1
        await run.until_idle()
    idle_turns, idle_requests = 0, list(clips)[:6]
    for cid in idle_requests:
        n_turns = len(run.turns)
        await run.play(np.concatenate([clip_audio[cid], silence(1.5)]))
        idle_turns += len(run.turns) - n_turns
    results["stop_phrases"] = f"{stop_ok}/{len(stops)} idled at once with no turn sent"
    results["requests_while_idle_sent"] = idle_turns
    results["requests_while_idle_played"] = len(idle_requests)

    mx.clear_cache()
    results["memory"] = {
        "footprint_mb_at_start": footprint_start,
        "footprint_mb_after": footprint_mb(),
        "mlx_active_mb": round(mx.get_active_memory() / 2**20),
        "mlx_peak_mb": round(mx.get_peak_memory() / 2**20),
        "budget_mb": 4096,
    }
    results["versions"] = {
        p: version(p) for p in ["parakeet-mlx", "openwakeword", "onnxruntime", "livekit", "mlx", "kokoro-mlx"]
    }
    results["machine"] = platform.machine()
    results["aec"] = args.aec
    results["settings"] = {"listening": Listening().__dict__, "timing": TIMING.__dict__}
    consumer.cancel()
    capture.close()
    args.out.write_text(json.dumps(results, indent=2) + "\n")
    if args.transcripts:
        args.transcripts.write_text("\n".join(text for _, text in run.turns) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
