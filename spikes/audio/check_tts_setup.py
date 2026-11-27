"""Synthesize EN/IT setup probes with the real engines and configured voices, without audio devices or conversational models.

    uv run python spikes/audio/check_tts_setup.py [qwen3|kokoro ...]

For each streamed sentence it reports when the first piece was ready and whether every later piece was ready before
the audio ahead of it had played out, which is what keeps speech free of gaps.
"""

from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from jarvis_hub.voice.language import Lang
from jarvis_hub.config import load_settings
from jarvis_hub.voice.tts import TTS_ENGINES, TtsEngine, make_synthesizer

PROBES: list[tuple[Lang, str]] = [
    ('en', 'The repository is ready, sir.'),
    ('it', 'Il repository è pronto, signore.'),
    ('en', 'The release is tagged and the changelog is up to date. Shall I announce it on the team channel?'),
    ('it', 'La release è pronta e il changelog è aggiornato. Vuole che lo annunci sul canale del team?'),
]


def check(engine: TtsEngine) -> dict[str, object]:
    def warn(error: str) -> None:
        print(f'{error}; checking the default instead.', file=sys.stderr)

    voice = load_settings(Path(__file__).resolve().parents[2], os.environ, ignore_voice=warn).voice
    synth = make_synthesizer(replace(voice, tts_engine=engine))
    began = time.perf_counter()
    synth.load()
    for lang in ('en', 'it'):
        for _ in synth.stream('Good evening.', synth.voice_for(lang)):
            pass
    load_ms = round((time.perf_counter() - began) * 1000)
    results: list[dict[str, object]] = []
    for lang, text in PROBES:
        voice = synth.voice_for(lang)
        start = time.perf_counter()
        first_ms, gaps, audio_s, words = None, 0, 0.0, []
        for said, samples in synth.stream(text, voice):
            ready = time.perf_counter() - start
            if first_ms is None:
                first_ms = round(ready * 1000)
            elif ready > first_ms / 1000 + audio_s:
                gaps += 1
            if not len(samples) or not np.isfinite(samples).all() or samples.dtype != np.float32:
                raise RuntimeError(f'{voice}: empty, nonfinite or non-float32 piece')
            audio_s += len(samples) / synth.sample_rate
            words.append(said)
        if ' '.join(w for w in words if w).split() != text.split():
            raise RuntimeError(f'{voice}: pieces do not carry the sentence\'s words')
        results.append({'voice': voice, 'first_piece_ms': first_ms, 'pieces': len(words), 'gaps': gaps,
                        'audio_s': round(audio_s, 2), 'total_ms': round((time.perf_counter() - start) * 1000)})
    return {'engine': engine, 'sample_rate': synth.sample_rate, 'load_and_warm_ms': load_ms, 'probes': results}


def main() -> None:
    engines: list[TtsEngine] = [e for e in TTS_ENGINES if e in sys.argv[1:]] or list(TTS_ENGINES)
    print(json.dumps([check(engine) for engine in engines], indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
