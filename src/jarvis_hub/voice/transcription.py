import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from jarvis_hub.voice.input import Transcriber
from jarvis_hub.voice.player import Samples


def _load_parakeet() -> Transcriber:
    from jarvis_hub.voice.engines import Parakeet

    return Parakeet()


class TranscriptionService:
    """Load and use one model on its own thread, shared by capture and uploaded Talks."""

    def __init__(self, load: Callable[[], Transcriber] = _load_parakeet) -> None:
        self._load = load
        self._model: Transcriber | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transcription")

    def transcribe(self, audio: Samples, *, after: float = 0.0) -> str:
        return self._executor.submit(self._transcribe, audio.copy(), after).result()

    async def transcribe_buffer(self, audio: Samples) -> str:
        return await asyncio.wrap_future(self._executor.submit(self._transcribe, audio.copy(), 0.0))

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _transcribe(self, audio: Samples, after: float) -> str:
        if self._model is None:
            self._model = self._load()
        return self._model.transcribe(audio, after=after)
