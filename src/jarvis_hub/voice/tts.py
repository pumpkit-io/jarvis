"""Text-to-speech: Kokoro on MLX in the Hub's own process, chosen in Phase 0 (docs/notes/phase-0-spikes.md, `## Audio`)."""

from pathlib import Path
import shutil
import tempfile
from typing import Any, Protocol, cast

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


class Synthesizer(Protocol):
    """Blocking calls; the caller keeps them all on one thread of its own."""

    @property
    def sample_rate(self) -> int: ...

    def load(self) -> None: ...

    def synthesize(self, text: str, voice: str) -> Samples:
        """Mono float samples of the whole text, at speed 1.0."""
        ...


class KokoroSynthesizer:
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

    def synthesize(self, text: str, voice: str) -> Samples:
        return self._tts.generate(text, voice=voice, speed=1.0, sample_rate=self.sample_rate).audio
