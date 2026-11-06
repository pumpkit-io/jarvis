"""Generate the frozen synthetic endpoint cases locally without opening audio devices."""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import soundfile as sf

from jarvis_hub.voice.tts import KokoroSynthesizer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, default=Path('spikes/audio/endpoint-heldout.json'))
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text())['cases']
    folder = args.out / 'pauses'
    folder.mkdir(parents=True, exist_ok=True)
    synth = KokoroSynthesizer()
    synth.load()
    manifest = []
    for case in cases:
        parts, segments, position = [], [], 0
        for segment in case['segments']:
            audio = synth.synthesize(segment['text'], 'am_liam' if case['lang'] == 'en' else 'im_nicola')
            active = np.flatnonzero(np.abs(audio) > .005)
            if not len(active):
                raise ValueError(f"{case['id']}: empty synthesis")
            audio = audio[max(0, int(active[0]) - 1200):min(len(audio), int(active[-1]) + 1200)]
            parts.append(audio)
            segments.append({'start_sample': position, 'end_sample': position + len(audio),
                             'endpoint_label': segment['endpoint_label']})
            position += len(audio)
            if segment['pause_after_s']:
                gap = np.zeros(round(segment['pause_after_s'] * 24000), np.float32)
                parts.append(gap)
                position += len(gap)
        path = folder / (case['id'] + '.wav')
        sf.write(path, np.concatenate(parts), 24000)
        manifest.append({'id': case['id'], 'segments': segments,
                         'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        print(case['id'], flush=True)
    (args.out / 'manifest.json').write_text(json.dumps({
        'sample_rate': 24000, 'source_sha256': hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        'method': 'Kokoro per segment; trim below .005 with 50ms padding; declared pauses appended',
        'cases': manifest,
    }, indent=2) + '\n')


if __name__ == '__main__':
    main()
