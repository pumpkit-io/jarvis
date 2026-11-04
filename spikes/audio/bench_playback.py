"""Opt-in silent-device playback probe; run from the repo root with `uv run python spikes/audio/bench_playback.py`."""

import argparse
import threading
import time
from unittest.mock import patch

import numpy as np
import sounddevice as sd

from jarvis_hub.voice.engines import Parakeet
from jarvis_hub.voice.player import Clip, SoundPlayer
from jarvis_hub.voice.tts import KokoroSynthesizer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-buffer", action="store_true", help="Replay the old low-latency setting.")
    args = parser.parse_args()
    events = []
    stream_factory = sd.OutputStream

    def silent_stream(**settings):
        callback = settings["callback"]
        if args.old_buffer:
            settings["latency"] = "low"

        def silent(out, frames, at, status):
            callback(out, frames, at, status)
            # Keep the real device clock and callback workload, but send no speech to the speakers.
            out[:] = 0
            events.append((time.perf_counter(), frames, at.outputBufferDacTime, bool(status.output_underflow)))

        settings["callback"] = silent
        stream = stream_factory(**settings)
        print(f"Actual output latency: {stream.latency:.6f} s", flush=True)
        return stream

    errors = []
    ready = threading.Event()
    go = threading.Event()

    def recognize():
        try:
            stt = Parakeet()
            noise = np.random.default_rng(0).normal(0, 0.01, 16_000).astype(np.float32)
            stt.transcribe(noise)
            ready.set()
            go.wait()
            for _ in range(12):
                stt.transcribe(noise)
        except Exception as e:
            errors.append(e)
            ready.set()

    tts = KokoroSynthesizer()
    tts.load()
    tts.synthesize("At your service, sir.", "am_liam")
    worker = threading.Thread(target=recognize, daemon=True)
    worker.start()
    assert ready.wait(60), "Recognition did not load"
    if errors:
        raise errors[0]
    with patch.object(sd, "OutputStream", silent_stream):
        player = SoundPlayer(tts.sample_rate)
    try:
        time.sleep(1)
        began = time.perf_counter()
        go.set()
        peak = 0.0
        for _ in range(6):
            samples = tts.synthesize(
                "At your service. I will look into that and report what I find, sir.", "am_liam"
            )
            assert np.all(np.isfinite(samples)), "Nonfinite synthesized samples"
            peak = max(peak, float(np.max(np.abs(samples))))
            player.play(Clip("Test.", "am_liam", samples, tts.sample_rate))
        worker.join(timeout=30)
        assert not worker.is_alive(), "Recognition did not finish"
        if errors:
            raise errors[0]
        time.sleep(1)
        rows = [row for row in events if row[0] >= began]
        missing = (rows[-1][2] - rows[0][2]) * tts.sample_rate - sum(row[1] for row in rows[:-1])
        print(f"Callbacks: {len(rows)}; block sizes: {sorted({row[1] for row in rows})}")
        print(f"Peak sample: {peak:.4f}; underrun flags: {sum(row[3] for row in rows)}")
        print(f"Net output-timeline deficit: {missing:.1f} samples ({missing / tts.sample_rate * 1000:.2f} ms)")
        assert peak < 1.0, "Synthesized audio clips"
        assert missing < tts.sample_rate * 0.01, "Device output timeline lost over 10 ms during inference"
    finally:
        player.close()


if __name__ == "__main__":
    main()
