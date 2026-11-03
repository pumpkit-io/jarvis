"""Render the corpus with macOS `say` voices into data/synthetic, mirroring the session layout.

Synthetic speech only proves the pipeline and gives speed numbers; accuracy on the
User's own voice comes from data/recordings.
"""

import json
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from common import SYNTHETIC, load_16k, save_16k
from corpus import CLIPS, PAUSE, PAUSE_TAKES, TAKES_PER_PHRASING, WAKE_PHRASINGS

CLIP_VOICE = {"en": "Samantha", "it": "Alice"}
EN_VOICES = ["Samantha", "Reed (English (US))", "Shelley (English (US))", "Eddy (English (US))", "Flo (English (US))",
             "Sandy (English (US))", "Rocko (English (US))", "Grandpa (English (US))", "Grandma (English (US))"]
IT_VOICES = ["Alice", "Reed (Italian (Italy))", "Shelley (Italian (Italy))", "Eddy (Italian (Italy))", "Flo (Italian (Italy))",
             "Sandy (Italian (Italy))", "Rocko (Italian (Italy))", "Grandpa (Italian (Italy))", "Grandma (Italian (Italy))"]


def say(text: str, voice: str, rate: int | None = None) -> np.ndarray:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.aiff"
        cmd = ["say", "-v", voice, "-o", str(out)]
        if rate:
            cmd += ["-r", str(rate)]
        subprocess.run(cmd + [text], check=True)
        return load_16k(out)


def pad(audio: np.ndarray, lead_s: float, tail_s: float, rng: np.random.Generator) -> np.ndarray:
    lead = np.zeros(int(lead_s * 16000), np.float32)
    tail = np.zeros(int(tail_s * 16000), np.float32)
    out = np.concatenate([lead, audio, tail])
    return out + rng.normal(0, 0.002, len(out)).astype(np.float32)


def main() -> None:
    rng = np.random.default_rng(0)
    manifest = {}
    for clip in CLIPS:
        audio = say(clip.text, CLIP_VOICE[clip.lang])
        save_16k(SYNTHETIC / "clips" / f"{clip.id}.wav", pad(audio, 0.2, 0.2, rng))
        manifest[clip.id] = {"lang": clip.lang, "kind": clip.kind, "text": clip.text, "seconds": round(len(audio) / 16000, 2)}
    for key, phrase in WAKE_PHRASINGS.items():
        voices = IT_VOICES if key == "ehi" else EN_VOICES
        for take in range(TAKES_PER_PHRASING):
            audio = say(phrase, voices[take % len(voices)], rate=int(rng.integers(150, 220)))
            save_16k(SYNTHETIC / "wake" / f"{key}_{take + 1:02d}.wav", pad(audio, 1.0, 1.0, rng))
    for take in PAUSE_TAKES:
        # `say` reads [[slnc ms]] as a silence of that length.
        audio = say(take.text.replace(PAUSE, " [[slnc 900]] "), CLIP_VOICE[take.lang])
        save_16k(SYNTHETIC / "pauses" / f"{take.id}.wav", pad(audio, 0.2, 0.2, rng))
    (SYNTHETIC / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"wrote {len(CLIPS)} clips, {len(WAKE_PHRASINGS) * TAKES_PER_PHRASING} wake takes and {len(PAUSE_TAKES)} pause takes to {SYNTHETIC}")


if __name__ == "__main__":
    main()
