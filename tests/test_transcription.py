import asyncio
import threading

import numpy as np
import pytest

from jarvis_hub.voice.engines import EngineChoice, load_engines
from jarvis_hub.voice.input import InputPipeline, Listening
from jarvis_hub.voice.player import Samples
from jarvis_hub.voice.state import Heard, InputEvent
from jarvis_hub.voice.transcription import TranscriptionService
from tests.test_input_pipeline import LoudVad, MarkerWake, ScriptedTurn


async def test_capture_and_uploaded_buffers_share_one_model_on_its_own_thread() -> None:
    loaded: list[int] = []
    calls: list[tuple[int, float]] = []

    class Model:
        def transcribe(self, audio: Samples, *, after: float = 0.0) -> str:
            calls.append((threading.get_ident(), after))
            return "Ciao, sir."

    def load() -> Model:
        loaded.append(threading.get_ident())
        return Model()

    service = TranscriptionService(load)
    try:
        assert loaded == []
        audio = np.zeros(1600, np.float32)
        assert await asyncio.to_thread(service.transcribe, audio, after=0.7) == "Ciao, sir."
        assert await service.transcribe_buffer(audio) == "Ciao, sir."
        assert len(loaded) == 1
        assert loaded[0] != threading.get_ident()
        assert calls == [(loaded[0], 0.7), (loaded[0], 0.0)]
    finally:
        service.close()


async def test_transcriptions_wait_their_turn_and_both_return() -> None:
    entered, release = threading.Event(), threading.Event()
    active = 0
    peak = 0

    class Model:
        def transcribe(self, audio: Samples, *, after: float = 0.0) -> str:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            entered.set()
            assert release.wait(timeout=2)
            active -= 1
            return f"Turn {int(audio[0])}"

    service = TranscriptionService(Model)
    try:
        first = asyncio.create_task(service.transcribe_buffer(np.ones(1600, np.float32)))
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(service.transcribe_buffer(np.full(1600, 2, np.float32)))
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.gather(first, second) == ["Turn 1", "Turn 2"]
        assert peak == 1
    finally:
        release.set()
        service.close()


async def test_mac_capture_and_upload_use_the_same_service(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis_hub.voice import engines

    loads: list[int] = []

    class Model:
        def __init__(self) -> None:
            loads.append(threading.get_ident())

        def transcribe(self, audio: Samples, *, after: float = 0.0) -> str:
            return "Open a PR"

    monkeypatch.setitem(engines.WAKE_ENGINES, "openwakeword", MarkerWake)
    monkeypatch.setattr(engines, "SileroVad", LoudVad)
    monkeypatch.setattr(engines, "SmartTurn", ScriptedTurn)
    service = TranscriptionService(Model)
    try:
        loaded = load_engines(EngineChoice(aec="off"), transcription=service)
        pipeline = InputPipeline(loaded, Listening(), lambda row: None)
        pipeline.capture(True)
        events: list[InputEvent] = []
        for i in range(160):
            frame = np.full(160, 1000 if i < 100 else 0, np.int16)
            events.extend(pipeline.push(frame, 100 + i / 100))
        assert [event.text for event in events if isinstance(event, Heard)] == ["Open a PR"]
        assert await service.transcribe_buffer(np.zeros(1600, np.float32)) == "Open a PR"
        assert len(loads) == 1
    finally:
        service.close()


async def test_buffer_only_transcription_does_not_load_microphone_models(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis_hub.voice import engines

    def microphone_model() -> None:
        pytest.fail("A buffer transcription must not load microphone models")

    class Model:
        def transcribe(self, audio: Samples, *, after: float = 0.0) -> str:
            return "Good evening"

    monkeypatch.setattr(engines, "OpenWakeWord", microphone_model)
    monkeypatch.setitem(engines.WAKE_ENGINES, "openwakeword", microphone_model)
    monkeypatch.setattr(engines, "SileroVad", microphone_model)
    monkeypatch.setattr(engines, "SmartTurn", microphone_model)
    monkeypatch.setattr(engines, "Parakeet", Model)
    service = TranscriptionService()
    try:
        assert await service.transcribe_buffer(np.zeros(1600, np.float32)) == "Good evening"
    finally:
        service.close()
