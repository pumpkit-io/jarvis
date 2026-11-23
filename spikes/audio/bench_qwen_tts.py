"""TTS: Qwen3-TTS 0.6B Base cloning the two reference clips, against Kokoro, for ADR 0029's go/no-go.

The Hub synthesises a whole sentence before playing it, so the number that matters is time to finished audio
per sentence, measured here for both engines the same way.

    uv run bench_qwen_tts.py [--model mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit]
"""

import argparse
import json
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from common import HERE, footprint_mb, p50, rss_mb, write_result

QWEN = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16"
REPO = HERE.parents[1]
REFERENCES = {
    "en": (REPO / "assets/voice/jarvis-en.wav", "English",
           "Of course. I've checked the three repositories, and everything is running smoothly. The only open "
           "question is whether we ship the new release tonight or wait until Monday."),
    "it": (REPO / "assets/voice/jarvis-it.wav", "Italian",
           "Certo. Ho controllato i tre repository e funziona tutto senza problemi. L'unica domanda aperta è se "
           "pubblichiamo la nuova versione stasera o aspettiamo lunedì."),
}
KOKORO_VOICES = {"en": "am_liam", "it": "im_nicola"}
SENTENCES = {
    "en": {"short": "Good evening, sir.",
           "one": "I have opened the pull request on pumpkit and checks are running.",
           "two": "The release is tagged and the changelog is up to date. Shall I announce it on the team channel?"},
    "it": {"short": "Buonasera, signore.",
           "one": "Ho aperto la pull request su pumpkit e i controlli sono partiti.",
           "two": "La release è pronta e il changelog è aggiornato. Vuole che lo annunci sul canale del team?"},
}
RUNS = 5
SR = 24_000
LISTEN = HERE / "data" / "qwen-tts"


def rms_db(audio: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(np.square(audio))) + 1e-12))


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def bench_qwen(model_id: str, temperature: float | None, seed: int | None, interval: float) -> dict:
    import mlx.core as mx
    from mlx_audio.tts.utils import load_model
    from mlx_audio.utils import load_audio

    mx.set_cache_limit(256 * 2**20)
    t0 = time.perf_counter()
    model = load_model(model_id)
    out: dict = {"model": model_id, "load_ms": round((time.perf_counter() - t0) * 1000),
                 "model_sample_rate": model.sample_rate, "temperature": temperature, "seed": seed, "streaming_interval_s": interval, "per_lang": {}}

    def say(lang: str, text: str, stream: bool = False):
        _, code, transcript = REFERENCES[lang]
        if seed is not None:
            mx.random.seed(seed)
        kwargs = {"temperature": temperature} if temperature is not None else {}
        return model.generate(text=text, ref_audio=refs[lang], ref_text=transcript, lang_code=code, stream=stream,
                              streaming_interval=interval, **kwargs)

    refs = {lang: load_audio(str(path), sample_rate=model.sample_rate) for lang, (path, _, _) in REFERENCES.items()}
    LISTEN.mkdir(parents=True, exist_ok=True)
    for lang, texts in SENTENCES.items():
        t = time.perf_counter()
        list(say(lang, texts["short"]))
        lang_out: dict = {"first_call_ms": round((time.perf_counter() - t) * 1000), "sentences": {}}
        for name, text in texts.items():
            times, rtfs, embeds, levels = [], [], [], []
            for run in range(RUNS):
                t = time.perf_counter()
                audio = np.concatenate([np.array(r.audio) for r in say(lang, text)]).astype(np.float32)
                took = time.perf_counter() - t
                times.append(took * 1000)
                rtfs.append(took / (len(audio) / model.sample_rate))
                levels.append(rms_db(audio))
                embeds.append(np.array(model.extract_speaker_embedding(mx.array(audio))).reshape(-1))
                sf.write(LISTEN / f"{lang}-{name}-{run}.wav", audio, model.sample_rate)
            reference = np.array(model.extract_speaker_embedding(refs[lang])).reshape(-1)
            lang_out["sentences"][name] = {
                "full_ms_p50": round(p50(times)), "full_ms_max": round(max(times)),
                "rtf_p50": round(p50(rtfs), 3), "rtf_max": round(max(rtfs), 3),
                "audio_seconds": round(len(audio) / model.sample_rate, 2),
                "rms_db_p50": round(p50(levels), 1),
                "speaker_similarity_to_reference_min": round(min(cosine(e, reference) for e in embeds), 3),
                "speaker_similarity_between_runs_min": round(min(cosine(a, b) for i, a in enumerate(embeds)
                                                                for b in embeds[i + 1:]), 3),
            }
        t = time.perf_counter()
        for i, _ in enumerate(say(lang, texts["two"], stream=True)):
            if i == 0:
                lang_out["stream_first_chunk_ms"] = round((time.perf_counter() - t) * 1000)
        out["per_lang"][lang] = lang_out
    out["reference_rms_db"] = {lang: round(rms_db(np.array(a)), 1) for lang, a in refs.items()}
    out["memory_mb"] = {"rss": round(rss_mb()), "footprint": footprint_mb(),
                        "mlx_active": round(mx.get_active_memory() / 2**20), "mlx_peak": round(mx.get_peak_memory() / 2**20)}
    return out


def bench_kokoro() -> dict:
    from kokoro_mlx import KokoroTTS
    from common import prepare_tts

    prepare_tts()
    tts = KokoroTTS.from_pretrained("mlx-community/Kokoro-82M-bf16")
    out: dict = {"per_lang": {}}
    for lang, texts in SENTENCES.items():
        voice = KOKORO_VOICES[lang]
        tts.generate(texts["short"], voice=voice)
        lang_out = {}
        for name, text in texts.items():
            times, levels = [], []
            for _ in range(RUNS):
                t = time.perf_counter()
                audio = np.asarray(tts.generate(text, voice=voice, speed=1.0, sample_rate=SR).audio, dtype=np.float32)
                times.append((time.perf_counter() - t) * 1000)
                levels.append(rms_db(audio))
            lang_out[name] = {"full_ms_p50": round(p50(times)), "rms_db_p50": round(p50(levels), 1)}
        out["per_lang"][lang] = lang_out
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=QWEN)
    ap.add_argument("--temperature", type=float)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--stream-interval", type=float, default=0.5)
    ap.add_argument("--engine", choices=["qwen3", "kokoro"], default="qwen3")
    args = ap.parse_args()
    if args.engine == "kokoro":
        result = {"engine": "kokoro", **bench_kokoro()}
    else:
        result = {"engine": "qwen3", **bench_qwen(args.model, args.temperature, args.seed, args.stream_interval)}
    name = "tts-kokoro-full" if args.engine == "kokoro" else f"tts-qwen3-{Path(args.model).name}"
    if args.temperature is not None or args.seed is not None:
        name += f"-t{args.temperature}-s{args.seed}"
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"-> {write_result(name, result)}")


if __name__ == "__main__":
    main()
