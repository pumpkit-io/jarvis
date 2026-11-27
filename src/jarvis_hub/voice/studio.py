"""`./jarvis voice`: design, hear and choose the voice packs Jarvis speaks with (ADR 0035).

Every model import is inside a factory, so the tests and `voice list` never load MLX.
"""

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Protocol, TextIO, cast
import wave

import numpy as np

from jarvis_hub.config import VoiceSettings
from jarvis_hub.voice.language import Lang
from jarvis_hub.voice.loudness import integrated_lufs
from jarvis_hub.voice.packs import (
    LANGUAGE_NAMES,
    LANGUAGES,
    SHIPPED_VOICES,
    VoicePack,
    VoicePackError,
    check_new_name,
    find_pack,
    list_packs,
    parse_language,
    save_pack,
)
from jarvis_hub.voice.player import Samples
from jarvis_hub.voice.tts import MLX_CACHE_LIMIT, SPEECH_LUFS, Cloner, Qwen3Synthesizer

# Designing runs once per voice, so the larger, better model; Jarvis then clones the take with its own 0.6B one.
DESIGN_MODEL = "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16"

DESCRIPTION = ("A calm, composed, deep adult male voice, mid-low pitch, precise diction, "
               "dry, like a refined AI butler. Medium pace, warm but understated.")
# Appended to every description, so each language is designed with a native accent.
ACCENTS: dict[Lang, str] = {
    "en": "Native speaker of American English, neutral accent.",
    "it": "Native speaker of standard Italian, neutral accent, no foreign accent.",
}
# What a design take says, and so its clone reference: about ten seconds, neutral, with varied sounds.
REFERENCE_TEXTS: dict[Lang, str] = {
    "en": "Of course. I've checked the three repositories, and everything is running smoothly. "
          "The only open question is whether we ship the new release tonight or wait until Monday.",
    "it": "Certo. Ho controllato i tre repository e funziona tutto senza problemi. "
          "L'unica domanda aperta è se pubblichiamo la nuova versione stasera o aspettiamo lunedì.",
}
# Typical replies, for hearing a voice and measuring its level; ADR 0032 measured over a handful like these.
SAMPLE_LINES: dict[Lang, tuple[str, ...]] = {
    "en": (
        "Good evening, sir. The build passed, and two pull requests are waiting for review.",
        "The release is tagged and the changelog is up to date. Shall I announce it on the team channel?",
        "I'm afraid the tests failed on the payments service. Shall I look into it?",
        "Done. I've merged the branch and deleted it.",
    ),
    "it": (
        "Buonasera. La build è passata e ci sono due pull request in attesa di revisione.",
        "La release è pronta e il changelog è aggiornato. Vuole che lo annunci sul canale del team?",
        "Temo che i test sul servizio pagamenti siano falliti. Vuole che ci dia un'occhiata?",
        "Fatto. Ho unito il branch e l'ho cancellato.",
    ),
}


class Designer(Protocol):
    @property
    def sample_rate(self) -> int: ...

    def design(self, text: str, language: str, instruct: str) -> Samples: ...


Play = Callable[[Samples, int], None]


@dataclass(frozen=True)
class Take:
    """A clip with the language and exact transcript it was spoken in; a design take keeps them in a sidecar."""

    clip: Path
    language: Lang
    transcript: str
    description: str | None = None
    model: str | None = None


def read_take(clip: Path) -> Take:
    sidecar = clip.with_suffix(".json")
    if not clip.is_file():
        raise VoicePackError(f"{clip} does not exist")
    if not sidecar.is_file():
        raise VoicePackError(f"{clip} is not a take from ./jarvis voice design; for any other recording use "
                             "./jarvis voice add <clip> --lang en|it --transcript \"...\" --name <name>")
    try:
        data: object = json.loads(sidecar.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict):
        raise VoicePackError(f"{sidecar} is not a take record")
    record = cast(dict[str, Any], data)
    language, transcript = parse_language(record.get("language")), record.get("transcript")
    if language is None or not isinstance(transcript, str):
        raise VoicePackError(f"{sidecar} is not a take record")
    description, model = record.get("description"), record.get("model")
    return Take(clip, language, transcript, description if isinstance(description, str) else None,
                model if isinstance(model, str) else None)


def write_wav(path: Path, samples: Samples, rate: int) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm.tobytes())


def set_env(path: Path, key: str, value: str) -> None:
    """Puts `KEY=value` where the first assignment of `key` was, or at the end, and drops any later ones."""
    assignment = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    kept: list[str] = []
    placed = False
    for line in lines:
        if not assignment.match(line):
            kept.append(line)
        elif not placed:
            kept.append(f"{key}={value}")
            placed = True
    if not placed:
        kept.append(f"{key}={value}")
    # `.env` may hold secrets: replace it whole, so a crash never leaves it half written, never readable by more
    # people than before, and through a symlink to the file it points at.
    target = path.resolve()
    mode = target.stat().st_mode & 0o777 if target.is_file() else 0o600
    staging = target.with_name(f".{target.name}.partial")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
        with os.fdopen(os.open(staging, flags, mode), "w", encoding="utf-8") as out:
            os.fchmod(out.fileno(), mode)
            out.write("\n".join(kept) + "\n")
        os.replace(staging, target)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise


def add_arguments(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="voice_command", required=True, metavar="{design,try,use,add,list}")
    design = commands.add_parser("design", help="generate candidate voices from a description and play each")
    design.add_argument("--lang", choices=LANGUAGES, default="en")
    design.add_argument("--takes", type=int, default=5, help="how many candidates (default 5)")
    design.add_argument("--describe", default=DESCRIPTION, help="the voice in plain words; a native accent is added")
    design.add_argument("--text", help="what each take says instead of the standard reference sentence")
    design.add_argument("--no-play", action="store_true", help="save the takes without playing them")
    trial = commands.add_parser("try", help="hear a take or a saved voice speak typical replies, as Jarvis would")
    trial.add_argument("voice", help="a take's WAV, or a voice name from ./jarvis voice list")
    use = commands.add_parser("use", help="make a take a voice and choose it for its language")
    use.add_argument("take", type=Path)
    use.add_argument("--name", required=True, help="lowercase letters, digits and hyphens")
    add = commands.add_parser("add", help="make your own recording a voice and choose it for its language")
    add.add_argument("clip", type=Path, help="about 10 seconds of clean, neutral speech by one speaker")
    add.add_argument("--lang", choices=LANGUAGES, required=True)
    add.add_argument("--transcript", required=True, help="exactly what the clip says, word for word")
    add.add_argument("--name", required=True, help="lowercase letters, digits and hyphens")
    commands.add_parser("list", help="show the voices and the one each language speaks")


class Studio:
    def __init__(
        self, voice: VoiceSettings, takes: Path, env: Path, out: TextIO,
        designer: Callable[[], Designer], cloner: Callable[[], Cloner], play: Play,
        *, environ: Mapping[str, str] | None = None, stamp: Callable[[], str] = lambda: time.strftime("%Y%m%d-%H%M%S"),
    ) -> None:
        self._voice, self._takes, self._env, self._out = voice, takes, env, out
        self._environ = environ or {}
        self._designer, self._cloner, self._play, self._stamp = designer, cloner, play, stamp

    def run(self, args: argparse.Namespace) -> None:
        match args.voice_command:
            case "design":
                self.design(args.lang, args.takes, args.describe, args.text, play=not args.no_play)
            case "try":
                self.trial(args.voice)
            case "use":
                self.choose(read_take(args.take), args.name)
            case "add":
                if not args.clip.is_file():
                    raise VoicePackError(f"{args.clip} does not exist")
                self.choose(Take(args.clip, args.lang, args.transcript), args.name)
            case _:
                self.show()

    def _say(self, line: str) -> None:
        self._out.write(line + "\n")
        self._out.flush()

    def design(self, lang: Lang, takes: int, describe: str, text: str | None, *, play: bool) -> None:
        if takes < 1:
            raise VoicePackError("--takes must be at least 1")
        instruct = f"{describe.strip()} {ACCENTS[lang]}"
        transcript = " ".join((text or REFERENCE_TEXTS[lang]).split())
        self._say(f"Designing {takes} {LANGUAGE_NAMES[lang]} voices with {DESIGN_MODEL} (first use downloads it):\n"
                  f"  {instruct}")
        designer = self._designer()
        self._takes.mkdir(parents=True, exist_ok=True)
        stamp = self._stamp()
        for i in range(1, takes + 1):
            clip = self._takes / f"take_{stamp}_{lang}_{i:02d}.wav"
            audio = designer.design(transcript, LANGUAGE_NAMES[lang], instruct)
            write_wav(clip, audio, designer.sample_rate)
            clip.with_suffix(".json").write_text(json.dumps(
                {"language": lang, "transcript": transcript, "description": instruct, "model": DESIGN_MODEL},
                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self._say(f"  {clip}")
            if play:
                self._play(audio, designer.sample_rate)
        self._say("\nHear one speak as Jarvis: ./jarvis voice try <take>\n"
                  "Make it Jarvis's voice:   ./jarvis voice use <take> --name <name>")

    def _audition(self, take: Take) -> tuple[list[Samples], float]:
        """The sample lines in the take's cloned voice, untrimmed, and the trim that brings them to SPEECH_LUFS."""
        with tempfile.TemporaryDirectory(prefix="jarvis-voice-") as folder:
            save_pack(Path(folder), "audition", take.clip, take.language, take.transcript, 0.0)
            synth = Qwen3Synthesizer({take.language: "audition"}, Path(folder), self._cloner)
            synth.load()
            lines = [np.concatenate([audio for _, audio in synth.stream(line, "audition")])
                     for line in SAMPLE_LINES[take.language]]
        return lines, SPEECH_LUFS - integrated_lufs(np.concatenate(lines), synth.sample_rate)

    def trial(self, voice: str) -> None:
        path = Path(voice)
        # Voice names have no dots, so a .wav is always a take.
        if path.suffix.lower() == ".wav":
            self._say(f"Cloning {path} with Jarvis's own model…")
            lines, trim = self._audition(read_take(path))
            self._say(f"It would speak at a {trim:+.1f} dB trim.")
            gain = 10 ** (trim / 20)
            for line in lines:
                self._play(np.clip(line * gain, -1.0, 1.0).astype(np.float32), Qwen3Synthesizer.sample_rate)
            self._say(f"Make it Jarvis's voice: ./jarvis voice use {path} --name <name>")
            return
        pack = find_pack(voice, self._voice.user_voices)
        self._say(f"Speaking as {pack.name} ({LANGUAGE_NAMES[pack.language]})…")
        synth = Qwen3Synthesizer({pack.language: pack.name}, self._voice.user_voices, self._cloner)
        synth.load()
        for line in SAMPLE_LINES[pack.language]:
            self._play(np.concatenate([audio for _, audio in synth.stream(line, pack.name)]), synth.sample_rate)
        self._say(f"To speak with it, set JARVIS_VOICE_{pack.language.upper()}={pack.name} in {self._env} and restart.")

    def choose(self, take: Take, name: str) -> None:
        if self._voice.user_voices is None:
            raise VoicePackError("no folder for your own voices")
        # Before the measurement, which takes minutes, so a bad name fails at once.
        check_new_name(name, self._voice.user_voices)
        self._say(f"Measuring {take.clip.name}'s level with Jarvis's own model…")
        _, trim = self._audition(take)
        pack = save_pack(self._voice.user_voices, name, take.clip, take.language, take.transcript, trim,
                         description=take.description, model=take.model)
        key = f"JARVIS_VOICE_{take.language.upper()}"
        set_env(self._env, key, name)
        self._say(f"Saved {pack.folder} at a {pack.trim_db:+.1f} dB trim.\n"
                  f"Set {key}={name} in {self._env}. Restart Jarvis to hear it.")
        if self._environ.get(key) and self._environ[key] != name:
            self._say(f"Your shell sets {key}={self._environ[key]}, which overrides .env: unset it to hear this voice.")

    def show(self) -> None:
        chosen = {self._voice.voice_en: "en", self._voice.voice_it: "it"}
        for name, pack in list_packs(self._voice.user_voices):
            if isinstance(pack, VoicePack):
                mark = "*" if chosen.get(name) == pack.language else " "
                where = "shipped" if pack.folder.parent == SHIPPED_VOICES else "yours"
                self._say(f"{mark} {name:<24} {pack.language}  {pack.trim_db:+.1f} dB  {where}")
            else:
                self._say(f"! {name:<24} unusable: {pack}")
        self._say("* marks the voice each language speaks (JARVIS_VOICE_EN, JARVIS_VOICE_IT).\n"
                  "Hear one: ./jarvis voice try <name>. Make a new one: ./jarvis voice design --lang en|it")


class MlxDesigner:
    def __init__(self) -> None:
        import mlx.core as mx
        from mlx_audio.tts.utils import load_model  # pyright: ignore[reportMissingTypeStubs, reportUnknownVariableType]

        mx.set_cache_limit(MLX_CACHE_LIMIT)
        self._model: Any = cast(Any, load_model)(DESIGN_MODEL)

    @property
    def sample_rate(self) -> int:
        return int(self._model.sample_rate)

    def design(self, text: str, language: str, instruct: str) -> Samples:
        results = self._model.generate_voice_design(text=text, language=language, instruct=instruct)
        return np.concatenate([np.asarray(r.audio, dtype=np.float32).reshape(-1) for r in results])


def play_on_speakers(samples: Samples, rate: int) -> None:
    import sounddevice as sd  # pyright: ignore[reportMissingTypeStubs]

    device = cast(Any, sd)
    device.play(samples, rate)
    device.wait()
