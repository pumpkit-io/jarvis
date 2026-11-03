"""`Capture` on its own thread, with stand-in engines and a scripted frame source: no microphone is opened."""

import asyncio
import threading

import numpy as np

from jarvis_hub.voice.engines import ReferenceBuffer
from jarvis_hub.voice.input import Capture, Engines, Listening, Push
from jarvis_hub.voice.player import Samples
from tests.fakes import until
from tests.test_input_pipeline import Everyone, LoudVad, MarkerWake, ScriptedStt, ScriptedTurn

FRAME = 160


class CountingEcho:
    def __init__(self) -> None:
        self.referenced = 0

    def process(self, frame: np.ndarray) -> np.ndarray:
        return frame

    def reference(self, samples: Samples, rate: int) -> None:
        self.referenced += len(samples)


class BlockingWake(MarkerWake):
    """Holds the capture thread on its first chunk until released, as a slow engine would."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def score(self, chunk: np.ndarray) -> float:
        self.release.wait(timeout=5)
        return 0.0


class Source:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.push: Push | None = None
        self.opened = self.closed = False

    def open(self, push: Push) -> None:
        if self.fail is not None:
            raise self.fail
        self.push, self.opened = push, True

    def close(self) -> None:
        self.closed = True

    def frames(self, n: int, value: int = 0, start: float = 0.0) -> None:
        assert self.push is not None
        for i in range(n):
            self.push(np.full(FRAME, value, np.int16), start + (i + 1) / 100)


def engines(echo: CountingEcho, wake: MarkerWake | None = None) -> Engines:
    return Engines(
        echo=echo, wake=wake or MarkerWake(), vad=LoudVad(), turn=ScriptedTurn(), stt=ScriptedStt(), gate=Everyone()
    )


async def test_nothing_played_reaches_echo_cancellation_when_the_microphone_cannot_open() -> None:
    echo, notices = CountingEcho(), list[str]()
    capture = Capture(lambda: engines(echo), Listening(), lambda row: None, notices.append, source=Source(OSError("busy")))
    capture.start()
    await until(lambda: notices != [])

    capture.reference(np.zeros(24_000, np.float32), 24_000)

    assert echo.referenced == 0
    assert notices == ["Listening is off: OSError: busy"]
    capture.close()


async def test_closing_while_the_engines_load_leaves_the_microphone_closed() -> None:
    loading, loaded = threading.Event(), threading.Event()
    echo, source = CountingEcho(), Source()

    def load() -> Engines:
        loading.set()
        loaded.wait(timeout=5)
        return engines(echo)

    capture = Capture(load, Listening(), lambda row: None, lambda text: None, source=source)
    capture.start()
    await asyncio.to_thread(loading.wait, 5)
    capture.close()
    loaded.set()
    await asyncio.sleep(0.05)

    assert not source.opened


async def test_a_microphone_that_gives_only_silence_is_reported_once() -> None:
    notices, source = list[str](), Source()
    capture = Capture(
        lambda: engines(CountingEcho()), Listening(), lambda row: None, notices.append, source=source, backlog_s=10
    )
    capture.start()
    await asyncio.to_thread(capture.ready.wait, 5)

    source.frames(600)
    await until(lambda: any("Microphone" in n for n in notices))
    await asyncio.sleep(0.05)

    assert len([n for n in notices if "Microphone" in n]) == 1
    capture.close()


async def test_a_quiet_room_is_not_a_silent_microphone() -> None:
    notices, source = list[str](), Source()
    capture = Capture(
        lambda: engines(CountingEcho()), Listening(), lambda row: None, notices.append, source=source, backlog_s=10
    )
    capture.start()
    await asyncio.to_thread(capture.ready.wait, 5)

    source.frames(600, value=3)
    await asyncio.sleep(0.1)

    assert not any("Microphone" in n for n in notices)
    capture.close()


async def test_a_capture_thread_that_falls_behind_drops_audio_rather_than_queueing_it_all() -> None:
    notices, source, wake = list[str](), Source(), BlockingWake()
    capture = Capture(
        lambda: engines(CountingEcho(), wake), Listening(), lambda row: None, notices.append, source=source, backlog_s=0.5
    )
    capture.start()
    await asyncio.to_thread(capture.ready.wait, 5)

    source.frames(500, value=3)
    await until(lambda: any("falling behind" in n for n in notices))
    wake.release.set()

    assert len([n for n in notices if "falling behind" in n]) == 1
    capture.close()


async def test_falling_behind_again_after_catching_up_is_reported_again() -> None:
    notices, source, wake = list[str](), Source(), BlockingWake()
    capture = Capture(
        lambda: engines(CountingEcho(), wake), Listening(), lambda row: None, notices.append, source=source, backlog_s=0.5
    )
    capture.start()
    await asyncio.to_thread(capture.ready.wait, 5)

    source.frames(500, value=3)
    await until(lambda: any("falling behind" in n for n in notices))
    wake.release.set()
    await asyncio.sleep(0.2)
    wake.release.clear()
    source.frames(500, value=3, start=5.0)
    await until(lambda: len([n for n in notices if "falling behind" in n]) == 2)
    wake.release.set()

    capture.close()


def test_the_echo_reference_keeps_only_the_latest_audio() -> None:
    buffer = ReferenceBuffer(max_s=1.0)
    for i in range(30):
        buffer.put(np.full(2400, i, np.float32), 24_000)

    taken = buffer.take()

    # Ten 100 ms blocks make the last second; the oldest were dropped.
    assert [int(samples[0]) for samples, _ in taken] == list(range(20, 30))
    assert buffer.take() == []
