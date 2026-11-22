"""Text-to-speech in the Hub's own process, on MLX: Qwen3-TTS cloning one reference clip per language (ADR 0029),
or Kokoro, the Phase 0 engine kept as the fallback (docs/notes/phase-0-spikes.md, `## Audio`).

Every engine import is inside `load()`, so text mode never pays for them.
"""

from collections.abc import Callable, Generator, Iterable, Iterator
from dataclasses import dataclass
import math
from pathlib import Path
import shutil
import tempfile
from typing import Any, Protocol, cast

import numpy as np

from jarvis_hub.config import TtsEngine
from jarvis_hub.voice.language import Lang
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
VOICE_ASSETS = Path(__file__).resolve().parents[3] / "assets" / "voice"
# Whole sentences take over a second; the first half-second piece is ready in about 0.4 s.
STREAM_INTERVAL_S = 0.5
# Both cloned voices measured about 2 dB louder than Kokoro, whose level the speech gate, barge-in thresholds and
# echo measurements were tuned on. A fixed gain, so pieces of one sentence keep their relative level.
GAIN = 10 ** (-2.0 / 20)
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


QWEN3_VOICES: dict[Lang, ClonedVoice] = {
    "en": ClonedVoice(
        "jarvis-en", VOICE_ASSETS / "jarvis-en.wav",
        "Of course. I've checked the three repositories, and everything is running smoothly. "
        "The only open question is whether we ship the new release tonight or wait until Monday.",
        "English",
    ),
    "it": ClonedVoice(
        "jarvis-it", VOICE_ASSETS / "jarvis-it.wav",
        "Certo. Ho controllato i tre repository e funziona tutto senza problemi. "
        "L'unica domanda aperta è se pubblichiamo la nuova versione stasera o aspettiamo lunedì.",
        "Italian",
    ),
}


class Cloner(Protocol):
    @property
    def sample_rate(self) -> int: ...

    def stream(self, text: str, voice: ClonedVoice) -> Iterable[Samples]: ...


class Qwen3Synthesizer:
    """Streams each sentence in pieces of about `STREAM_INTERVAL_S`, cloning the voice of its language."""

    sample_rate = 24_000

    def __init__(
        self, voices: dict[Lang, ClonedVoice] = QWEN3_VOICES, load_model: Callable[[], Cloner] | None = None
    ) -> None:
        self._voices = voices
        self._by_name = {voice.name: voice for voice in voices.values()}
        self._load_model = load_model or _MlxQwen3
        self._model: Cloner | None = None

    def load(self) -> None:
        missing = [str(voice.clip) for voice in self._voices.values() if not voice.clip.is_file()]
        if missing:
            raise FileNotFoundError(
                f"missing reference clip {', '.join(missing)}; restore it from git or set JARVIS_TTS_ENGINE=kokoro"
            )
        self._model = self._load_model()

    def voice_for(self, lang: Lang) -> str:
        return self._voices[lang].name

    def stream(self, text: str, voice: str) -> Generator[Piece]:
        if self._model is None:
            raise RuntimeError("Qwen3Synthesizer.stream before load()")
        if voice not in self._by_name:
            raise ValueError(f"unknown voice {voice!r}; this engine speaks {', '.join(self._by_name)}")
        model = self._model
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
            yield " ".join(words[said:upto]), np.clip(audio * GAIN, -1.0, 1.0).astype(np.float32)
            said = upto
        if said < len(words):
            yield " ".join(words[said:]), np.zeros(round(TAIL_S * self.sample_rate), dtype=np.float32)


def _resample(audio: Samples, src: int, dst: int) -> Samples:
    if src == dst:
        return audio
    from scipy import signal  # pyright: ignore[reportMissingTypeStubs]

    g = math.gcd(src, dst)
    return np.asarray(cast(Any, signal).resample_poly(audio, dst // g, src // g), dtype=np.float32)


class _MlxQwen3:
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

        if voice.name not in self._references:
            self._references[voice.name] = load_audio(str(voice.clip), sample_rate=self.sample_rate)
        # Newlines would split the text into separately generated segments.
        for chunk in self._model.generate(
            text=" ".join(text.split()), ref_audio=self._references[voice.name], ref_text=voice.transcript,
            lang_code=voice.language, stream=True, streaming_interval=STREAM_INTERVAL_S,
        ):
            yield np.asarray(chunk.audio, dtype=np.float32)


TTS_ENGINES: dict[TtsEngine, Callable[[], Synthesizer]] = {"qwen3": Qwen3Synthesizer, "kokoro": KokoroSynthesizer}


def make_synthesizer(engine: TtsEngine) -> Synthesizer:
    return TTS_ENGINES[engine]()
