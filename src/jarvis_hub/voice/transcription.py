import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import Future
from queue import Empty, Queue

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
        self._jobs: Queue[tuple[Future[str], Samples, float] | None] = Queue()
        self._lock = threading.Lock()
        self._closed = False
        # A stalled model download or inference must not keep Jarvis alive after its terminal closes.
        self._worker = threading.Thread(target=self._run, name="transcription", daemon=True)
        self._worker.start()

    def transcribe(self, audio: Samples, *, after: float = 0.0) -> str:
        return self._submit(audio, after).result()

    async def transcribe_buffer(self, audio: Samples) -> str:
        return await asyncio.wrap_future(self._submit(audio, 0.0))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            while True:
                try:
                    job = self._jobs.get_nowait()
                except Empty:
                    break
                if job is not None:
                    job[0].cancel()
            self._jobs.put(None)

    def _submit(self, audio: Samples, after: float) -> Future[str]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Transcription service is closed")
            result: Future[str] = Future()
            self._jobs.put((result, audio.copy(), after))
            return result

    def _run(self) -> None:
        while (job := self._jobs.get()) is not None:
            result, audio, after = job
            with self._lock:
                if self._closed:
                    result.cancel()
                    continue
                if not result.set_running_or_notify_cancel():
                    continue
            try:
                text = self._transcribe(audio, after)
            except BaseException as error:
                result.set_exception(error)
            else:
                result.set_result(text)

    def _transcribe(self, audio: Samples, after: float) -> str:
        if self._model is None:
            self._model = self._load()
        return self._model.transcribe(audio, after=after)
