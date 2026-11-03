"""Text-to-speech: Kokoro on MLX in the Hub's own process, chosen in Phase 0 (docs/notes/phase-0-spikes.md, `## Audio`)."""

from typing import Any, Protocol

from jarvis_hub.voice.player import Samples

KOKORO_MODEL = "mlx-community/Kokoro-82M-bf16"
# Freed buffers otherwise swell the process past the audio budget (4.8 GB in the Phase 0 TTS run).
MLX_CACHE_LIMIT = 256 * 2**20


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

        mx.set_cache_limit(MLX_CACHE_LIMIT)
        self._tts = KokoroTTS.from_pretrained(KOKORO_MODEL)

    def synthesize(self, text: str, voice: str) -> Samples:
        return self._tts.generate(text, voice=voice, speed=1.0, sample_rate=self.sample_rate).audio
