"""TTS: time to first audio for a 12-word sentence, voices present, speed 1.0.

    uv run bench_tts.py
"""

import json
import os
import time

import httpx

from common import footprint_mb, p50, rss_mb, write_result
from corpus import TTS_SENTENCES

VOICES = {"en": "am_liam", "it": "im_nicola"}
RUNS = 5
SERVER = "http://127.0.0.1:8880/v1"


def bench_inprocess() -> dict:
    import mlx.core as mx
    from kokoro_mlx import KokoroTTS
    from common import prepare_tts

    prepare_tts()

    t0 = time.perf_counter()
    tts = KokoroTTS.from_pretrained("mlx-community/Kokoro-82M-bf16")
    load_ms = (time.perf_counter() - t0) * 1000
    voices = tts.list_voices()
    out = {"engine": "kokoro-mlx 0.1.4 in-process (generate_stream)", "load_ms": round(load_ms),
           "voices_present": {v: v in voices for v in VOICES.values()}, "per_lang": {}}
    for lang, text in TTS_SENTENCES.items():
        voice = VOICES[lang]
        next(tts.generate_stream(text, voice=voice, speed=1.0))
        first, total, audio_s = [], [], 0.0
        for _ in range(RUNS):
            t = time.perf_counter()
            chunks = []
            for i, chunk in enumerate(tts.generate_stream(text, voice=voice, speed=1.0)):
                if i == 0:
                    first.append((time.perf_counter() - t) * 1000)
                chunks.append(len(chunk))
            total.append((time.perf_counter() - t) * 1000)
            audio_s = sum(chunks) / tts.SAMPLE_RATE
        out["per_lang"][lang] = {"voice": voice, "first_audio_ms_p50": round(p50(first), 1),
                                 "full_ms_p50": round(p50(total), 1), "audio_seconds": round(audio_s, 2),
                                 "chunks": len(chunks)}
    out["memory_mb"] = {"rss": round(rss_mb()), "footprint": footprint_mb(),
                        "mlx_active": round(mx.get_active_memory() / 2**20), "mlx_peak": round(mx.get_peak_memory() / 2**20)}
    return out


def server_pid() -> int | None:
    out = os.popen("lsof -t -iTCP:8880 -sTCP:LISTEN").read().split()
    return int(out[0]) if out else None


def bench_server() -> dict:
    pid = server_pid()
    if not pid:
        return {"engine": "VoiceMode Kokoro server :8880", "skipped": "not running"}
    client = httpx.Client(timeout=60)
    voices = client.get(f"{SERVER}/audio/voices").json()["voices"]
    out = {"engine": "VoiceMode Kokoro-FastAPI server :8880 (streamed pcm)",
           "voices_present": {v: v in voices for v in VOICES.values()}, "per_lang": {}}
    for lang, text in TTS_SENTENCES.items():
        body = {"model": "kokoro", "input": text, "voice": VOICES[lang], "speed": 1.0,
                "response_format": "pcm", "stream": True}
        first, total, cold = [], [], None
        for run in range(RUNS + 1):
            t = time.perf_counter()
            got_first = None
            n = 0
            with client.stream("POST", f"{SERVER}/audio/speech", json=body) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_bytes():
                    if chunk and got_first is None:
                        got_first = (time.perf_counter() - t) * 1000
                    n += len(chunk)
            if not run:
                cold = round(got_first, 1)
            else:
                first.append(got_first)
                total.append((time.perf_counter() - t) * 1000)
        out["per_lang"][lang] = {"voice": VOICES[lang], "first_audio_ms_p50": round(p50(first), 1), "first_request_ms": cold,
                                 "full_ms_p50": round(p50(total), 1), "audio_seconds": round(n / 2 / 24000, 2)}
    out["memory_mb"] = {"rss": round(rss_mb(pid)), "footprint": footprint_mb(pid), "note": "server process"}
    # VoiceMode's launchd plist sets UVICORN_LIMIT_MAX_REQUESTS=25, so the server restarts every 25 requests.
    out["requests_made"] = 1 + 2 * (RUNS + 1)
    return out


def main() -> None:
    result = {"sentences": TTS_SENTENCES, "speed": 1.0, "server": bench_server(), "inprocess": bench_inprocess()}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"-> {write_result('tts', result)}")


if __name__ == "__main__":
    main()
