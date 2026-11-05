"""Synthesize EN/IT setup probes without audio devices or conversational models."""

import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from jarvis_hub.voice.tts import KokoroSynthesizer


def main() -> None:
    synth = KokoroSynthesizer()
    synth.load()
    results = []
    for voice, text in (
        ('am_liam', 'The repository is ready, sir.'),
        ('im_nicola', 'Il repository è pronto, signore.'),
    ):
        audio = synth.synthesize(text, voice)
        if not len(audio) or not np.isfinite(audio).all():
            raise RuntimeError(f'{voice}: empty or nonfinite synthesis')
        results.append({'voice': voice, 'samples': len(audio), 'finite': True})
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
