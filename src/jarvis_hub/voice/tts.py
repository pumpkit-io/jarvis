"""Text-to-speech in the Hub's own process, on MLX: Qwen3-TTS cloning one voice pack per language (ADR 0029, 0035),
or Kokoro, the Phase 0 engine kept as the fallback (docs/notes/phase-0-spikes.md, `## Audio`).

Every engine import is inside `load()`, so text mode never pays for them.
"""

from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from dataclasses import dataclass
import math
from pathlib import Path
import shutil
import tempfile
from typing import Any, Protocol, cast

import numpy as np

from jarvis_hub.config import TtsEngine, VoiceSettings
from jarvis_hub.voice.language import Lang
from jarvis_hub.voice.packs import DEFAULT_VOICES, LANGUAGE_NAMES, VoicePackError, find_pack
from jarvis_hub.voice.player import Samples

KOKORO_MODEL = "mlx-community/Kokoro-82M-bf16"
# Freed buffers otherwise swell the process past the audio budget (4.8 GB in the Phase 0 TTS run).
MLX_CACHE_LIMIT = 256 * 2**20


_ESPEAK_COPIES: dict[Path, tempfile.TemporaryDirectory[str]] = {}


def configure_espeak() -> None:
    """Keep the bundled native data path within eSpeak's fixed-size buffer."""
    import espeakng_loader  # pyright: ignore[reportMissingTypeStubs]

    repair = "Run uv sync --locked --reinstall-package espeakng-loader in this checkout."
    try:
        source = Path(cast(Any, espeakng_loader).get_data_path())
        for resource in ("phontab", "phonindex", "phondata", "en_dict", "it_dict"):
            if not (source / resource).is_file():
                raise RuntimeError(f"Missing eSpeak resource {source / resource}")
        data = source
        # The native resolver also tries appending /espeak-ng-data to its 160-byte buffer.
        if len(str(source).encode()) >= 140:
            if source not in _ESPEAK_COPIES:
                directory = tempfile.TemporaryDirectory(prefix="jarvis-espeak-", dir="/tmp")
                try:
                    shutil.copytree(source, Path(directory.name) / "espeak-ng-data")
                except OSError:
                    directory.cleanup()
                    raise
                _ESPEAK_COPIES[source] = directory
            data = Path(_ESPEAK_COPIES[source].name) / "espeak-ng-data"
        from misaki import espeak  # pyright: ignore[reportMissingTypeStubs]

        cast(Any, espeak).EspeakWrapper.set_data_path(str(data))
    except (OSError, RuntimeError) as error:
        raise RuntimeError(f"Cannot prepare eSpeak speech resources: {error}. {repair}") from error


# One piece of a sentence: the words it says and their audio.
Piece = tuple[str, Samples]


class Synthesizer(Protocol):
    """Blocking calls; the caller keeps them all on one thread of its own, including each step of a stream."""

    @property
    def sample_rate(self) -> int: ...

    def load(self) -> None: ...

    def voice_for(self, lang: Lang) -> str:
        ...

    def stream(self, text: str, voice: str) -> Generator[Piece]:
        """Mono float32 pieces at `sample_rate`, at speed 1.0, that played back to back say the whole text.
        Their words, joined with spaces, are the text's words."""
        ...


# American English only: never a British voice (bf_*, bm_*).
KOKORO_VOICES: dict[Lang, str] = {"en": "am_liam", "it": "im_nicola"}


class KokoroSynthesizer:
    """Synthesises a sentence in one piece, about 0.1 s each."""

    sample_rate = 24_000

    def __init__(self) -> None:
        self._tts: Any = None

    def load(self) -> None:
        # MLX ties its streams to the thread that made them, so load and synthesise on the same thread.
        import mlx.core as mx
        from kokoro_mlx import KokoroTTS  # pyright: ignore[reportMissingTypeStubs]

        configure_espeak()
        mx.set_cache_limit(MLX_CACHE_LIMIT)
        self._tts = KokoroTTS.from_pretrained(KOKORO_MODEL)

    def voice_for(self, lang: Lang) -> str:
        return KOKORO_VOICES[lang]

    def synthesize(self, text: str, voice: str) -> Samples:
        return self._tts.generate(text, voice=voice, speed=1.0, sample_rate=self.sample_rate).audio

    def stream(self, text: str, voice: str) -> Generator[Piece]:
        yield " ".join(text.split()), self.synthesize(text, voice)


# 8-bit: bf16 put the audio stack over its 4 GB budget and was slower (ADR 0029).
QWEN3_MODEL = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit"
# Whole sentences take over a second; the first half-second piece is ready in about 0.4 s.
STREAM_INTERVAL_S = 0.5
# Kokoro's English voice speaks at this level, on which the speech gate, barge-in thresholds and echo measurements
# were tuned. Every cloned voice is trimmed to it, and the iPhone raises it to its own target (ADR 0032).
SPEECH_LUFS = -25.0
# Within the 15 to 18.5 both voices speak, so a piece's words are seldom early or late.
CHARS_PER_SECOND = 17.0
# Carries words the estimate had not reached when the audio ended.
TAIL_S = 0.02


@dataclass(frozen=True)
class ClonedVoice:
    """A reference clip and its exact transcript; Qwen3-TTS clones the speaker, accent and tone from them."""

    name: str
    clip: Path
    transcript: str
    language: str
    # Brings this voice to SPEECH_LUFS; fixed, so pieces of one sentence keep their relative level.
    trim_db: float


class Cloner(Protocol):
    @property
    def sample_rate(self) -> int: ...

    def stream(self, text: str, voice: ClonedVoice) -> Iterable[Samples]: ...


class Qwen3Synthesizer:
    """Streams each sentence in pieces of about `STREAM_INTERVAL_S`, cloning the voice pack chosen for its language."""

    sample_rate = 24_000

    def __init__(
        self, voices: Mapping[Lang, str] = DEFAULT_VOICES, user_voices: Path | None = None,
        load_model: Callable[[], Cloner] | None = None,
    ) -> None:
        self._names = dict(voices)
        self.user_voices = user_voices
        self._by_name: dict[str, ClonedVoice] = {}
        self._load_model = load_model or MlxQwen3
        self._model: Cloner | None = None

    def load(self) -> None:
        for lang, name in self._names.items():
            setting = f"JARVIS_VOICE_{lang.upper()}={name}"
            try:
                pack = find_pack(name, self.user_voices)
            except VoicePackError as e:
                raise VoicePackError(f"{setting}: {e} Or set JARVIS_TTS_ENGINE=kokoro.") from None
            if pack.language != lang:
                raise VoicePackError(f"{setting}: voice {name!r} speaks {LANGUAGE_NAMES[pack.language]}, "
                                     f"not {LANGUAGE_NAMES[lang]}")
            self._by_name[name] = ClonedVoice(name, pack.clip, pack.transcript, LANGUAGE_NAMES[lang], pack.trim_db)
        self._model = self._load_model()

    def voice_for(self, lang: Lang) -> str:
        return self._names[lang]

    def stream(self, text: str, voice: str) -> Generator[Piece]:
        if self._model is None:
            raise RuntimeError("Qwen3Synthesizer.stream before load()")
        if voice not in self._by_name:
            raise ValueError(f"unknown voice {voice!r}; this engine speaks {', '.join(self._by_name)}")
        model = self._model
        gain = 10 ** (self._by_name[voice].trim_db / 20)
        words = text.split()
        said, spoken_s = 0, 0.0
        for piece in model.stream(text, self._by_name[voice]):
            audio = _resample(np.asarray(piece, dtype=np.float32).reshape(-1), model.sample_rate, self.sample_rate)
            if not len(audio):
                continue
            spoken_s += len(audio) / self.sample_rate
            upto = said
            while upto < len(words) and len(" ".join(words[: upto + 1])) <= spoken_s * CHARS_PER_SECOND:
                upto += 1
            yield " ".join(words[said:upto]), np.clip(audio * gain, -1.0, 1.0).astype(np.float32)
            said = upto
        if said < len(words):
            yield " ".join(words[said:]), np.zeros(round(TAIL_S * self.sample_rate), dtype=np.float32)


def _resample(audio: Samples, src: int, dst: int) -> Samples:
    if src == dst:
        return audio
    from scipy import signal  # pyright: ignore[reportMissingTypeStubs]

    g = math.gcd(src, dst)
    return np.asarray(cast(Any, signal).resample_poly(audio, dst // g, src // g), dtype=np.float32)


class MlxQwen3:
    """mlx-audio's Qwen3-TTS, which caches each clip's encoded speech after its first use."""

    def __init__(self) -> None:
        # MLX ties its streams to the thread that made them, so this runs on the synthesis thread.
        import mlx.core as mx
        from mlx_audio.tts.utils import load_model  # pyright: ignore[reportMissingTypeStubs, reportUnknownVariableType]

        mx.set_cache_limit(MLX_CACHE_LIMIT)
        # mlx-audio types the model as a Path but takes a Hugging Face id, as its own examples do.
        self._model: Any = cast(Any, load_model)(QWEN3_MODEL)
        self._references: dict[str, Any] = {}

    @property
    def sample_rate(self) -> int:
        return int(self._model.sample_rate)

    def stream(self, text: str, voice: ClonedVoice) -> Iterator[Samples]:
        from mlx_audio.utils import load_audio  # pyright: ignore[reportMissingTypeStubs, reportUnknownVariableType]

        # By clip, not name: `./jarvis voice` auditions every take under the same name.
        if str(voice.clip) not in self._references:
            self._references[str(voice.clip)] = load_audio(str(voice.clip), sample_rate=self.sample_rate)
        # Newlines would split the text into separately generated segments.
        for chunk in self._model.generate(
            text=" ".join(text.split()), ref_audio=self._references[str(voice.clip)], ref_text=voice.transcript,
            lang_code=voice.language, stream=True, streaming_interval=STREAM_INTERVAL_S,
        ):
            yield np.asarray(chunk.audio, dtype=np.float32)


def _qwen3(voice: VoiceSettings) -> Synthesizer:
    return Qwen3Synthesizer({"en": voice.voice_en, "it": voice.voice_it}, voice.user_voices)


def _kokoro(voice: VoiceSettings) -> Synthesizer:
    return KokoroSynthesizer()


TTS_ENGINES: dict[TtsEngine, Callable[[VoiceSettings], Synthesizer]] = {"qwen3": _qwen3, "kokoro": _kokoro}


def make_synthesizer(voice: VoiceSettings) -> Synthesizer:
    return TTS_ENGINES[voice.tts_engine](voice)
