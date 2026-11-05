"""`Capture` on its own thread, with stand-in engines and a scripted frame source: no microphone is opened."""

import asyncio
import threading
from pathlib import Path
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from jarvis_hub.voice.clips import ClipSettings, ClipStore
from jarvis_hub.voice.engines import NoEchoCancellation, ReferenceBuffer, WebRtcEchoCancellation
from jarvis_hub.voice.input import Capture, Engines, Listening, Push
from jarvis_hub.voice.player import Samples
from jarvis_hub.voice.state import Heard
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


async def test_failed_clip_retention_disables_recording_and_keeps_listening(tmp_path: Path) -> None:
    class UnprunableClips(ClipStore):
        def prune(self) -> None:
            raise PermissionError("clip cannot be removed")

    notices, source = list[str](), Source()
    folder = tmp_path / "clips"
    clips = UnprunableClips(folder, ClipSettings(enabled=True))
    capture = Capture(
        lambda: engines(CountingEcho()), Listening(), lambda row: None, notices.append,
        source=source, backlog_s=10, clips=clips,
    )
    capture.start()
    try:
        await until(capture.ready.is_set)
        capture.capture(True)
        source.frames(80, value=1000)
        source.frames(200, start=0.8)
        async with asyncio.timeout(1):
            async for event in capture.events():
                if isinstance(event, Heard):
                    assert event.text == "Open a PR"
                    break
        assert "Audio clips are off for this run: retention failed: PermissionError: clip cannot be removed" in notices
        assert not folder.exists()
    finally:
        capture.close()
    assert source.closed


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


async def test_trace_preserves_capture_and_playback_times_through_an_inference_stall() -> None:
    from tests.test_player import FakeStream
    from jarvis_hub.voice.player import Clip, SoundPlayer

    records: list[dict[str, Any]] = []
    echo, source, wake = CountingEcho(), Source(), BlockingWake()
    now = [100.0]
    capture = Capture(
        lambda: engines(echo, wake), Listening(), records.append, lambda text: None,
        source=source, backlog_s=10, clock=lambda: now[0],
    )
    streams: list[FakeStream] = []

    def open_stream(rate: int, fill: Callable[[np.ndarray], None]) -> FakeStream:
        streams.append(FakeStream(fill))
        return streams[-1]

    player = SoundPlayer(24_000, open_stream=open_stream, on_played=capture.reference)
    capture.start()
    try:
        await until(capture.ready.is_set)
        source.frames(8, value=3, start=99.92)
        await asyncio.sleep(0.02)
        player.play(Clip("Synthetic playback", "test", np.full(2400, 0.25, np.float32), 24_000))
        np.testing.assert_array_equal(streams[0].pull(1024), 0.25)
        now[0] = 101.0
        source.frames(5, value=3, start=100.0)
        wake.release.set()
        await until(lambda: any(r.get("kind") == "capture" and r.get("captured_through_at") == 100.05 for r in records))

        frames = [r for r in records if r["kind"] == "capture"]
        assert frames[-1]["max_queue_delay_ms"] == 990
        assert frames[-1]["processed_at"] == 101.0
        reference = next(r for r in records if r["kind"] == "playback_reference")
        assert reference["submitted_at"] == 100.0
        assert reference["samples"] == 1024
        assert reference["device_played_at"] is None
        assert echo.referenced == 1024
        assert all("audio" not in r and "text" not in r for r in records)
    finally:
        wake.release.set()
        capture.close()
        player.close()


@pytest.mark.parametrize("overlap", [False, True])
@pytest.mark.parametrize("headphones", [False, True])
async def test_playback_is_removed_while_user_speech_survives_an_inference_stall(
    monkeypatch: pytest.MonkeyPatch, overlap: bool, headphones: bool,
) -> None:
    from jarvis_hub.voice.player import Clip, SoundPlayer
    from jarvis_hub.voice.state import BargeIn, WakeWord
    from tests.test_player import FakeStream

    from collections import deque
    from livekit import rtc

    class AudioFrame:
        def __init__(self, data: bytes, rate: int, channels: int, samples: int) -> None:
            self.data = bytearray(data)

    monkeypatch.setattr(rtc, 'AudioFrame', AudioFrame)

    class AlignedApm:
        def __init__(self, **settings: Any) -> None:
            self.reference_frames: deque[np.ndarray] = deque()
            self.output: list[np.ndarray] = []

        def process_reverse_stream(self, frame: rtc.AudioFrame) -> None:
            pcm = np.frombuffer(bytes(frame.data), np.int16)
            self.reference_frames.append(np.repeat(pcm[::3], 2))

        def process_stream(self, frame: rtc.AudioFrame) -> None:
            pcm = np.frombuffer(frame.data, np.int16)
            if self.reference_frames:
                pcm[:] -= self.reference_frames.popleft()
            self.output.append(pcm.copy())

    apms: list[AlignedApm] = []

    def apm(**settings: Any) -> AlignedApm:
        apms.append(AlignedApm(**settings))
        return apms[-1]

    monkeypatch.setattr(rtc, 'AudioProcessingModule', apm)

    class StalledWake(MarkerWake):
        def __init__(self) -> None:
            self.entered, self.release = threading.Event(), threading.Event()
            self.first = True

        def score(self, chunk: np.ndarray) -> float:
            if self.first:
                self.first = False
                self.entered.set()
                self.release.wait(timeout=5)
            return super().score(chunk)

    echo = NoEchoCancellation() if headphones else WebRtcEchoCancellation()
    source, wake = Source(), StalledWake()
    heard: list[object] = []
    notices: list[str] = []
    records: list[dict[str, Any]] = []
    stt = ScriptedStt(words={1000: 'User correction', 6553: 'Jarvis echo'})
    capture = Capture(
        lambda: Engines(echo, wake, LoudVad(), ScriptedTurn(), stt, Everyone()),
        Listening(), records.append, notices.append, source=source, backlog_s=3,
    )
    streams: list[FakeStream] = []

    def open_stream(rate: int, fill: Callable[[np.ndarray], None]) -> FakeStream:
        streams.append(FakeStream(fill))
        return streams[-1]

    player = SoundPlayer(24_000, open_stream=open_stream, on_played=capture.reference)
    capture.start()
    async def collect() -> None:
        async for event in capture.events():
            heard.append(event)
    collecting = asyncio.create_task(collect())
    try:
        await until(capture.ready.is_set)
        capture.capture(True)
        capture.barge_in(0.0)
        source.frames(8, value=3)
        assert await asyncio.to_thread(wake.entered.wait, 2)
        player.play(Clip('First synthetic sentence.', 'test', np.full(12_480, .2, np.float32), 24_000))
        player.play(Clip('Second synthetic sentence.', 'test', np.full(12_480, .4, np.float32), 24_000))
        assert source.push is not None
        at = .08
        for block in range(35):
            if block == 22:
                player.stop()
            out = streams[0].pull(960)
            for pcm in (out * 32767).astype(np.int16).reshape(-1, 240):
                at = round(at + .01, 6)
                captured = np.zeros(160, np.int16) if headphones else np.repeat(pcm[::3], 2)
                if overlap and 8 <= block < 18:
                    captured += 1000
                source.push(captured, at)
        wake.release.set()
        await until(lambda: any(row.get('captured_through_at') == at for row in records)
                    or any('Could not' in notice for notice in notices))
        assert not any('Could not' in notice for notice in notices), notices
        if overlap:
            await until(lambda: any(isinstance(e, Heard) for e in heard))
            assert [e.text for e in heard if isinstance(e, Heard)] == ['User correction']
            if not headphones:
                assert all(np.isin(frame, [0, 1000]).all() for frame in apms[0].output[8:])
            return
        assert not any(isinstance(e, (Heard, BargeIn, WakeWord)) for e in heard)
        if not headphones:
            assert not any(frame.any() for frame in apms[0].output[8:])

        source.frames(40, value=1000, start=at)
        source.frames(80, start=at + .4)
        await until(lambda: any(isinstance(e, Heard) for e in heard))
        assert [e.text for e in heard if isinstance(e, Heard)] == ['User correction']
    finally:
        wake.release.set()
        capture.close()
        player.close()
        collecting.cancel()
        try:
            await collecting
        except asyncio.CancelledError:
            pass


async def test_successful_cancellation_does_not_report_a_silent_microphone() -> None:
    class RemovedPlayback(CountingEcho):
        def process(self, frame: np.ndarray) -> np.ndarray:
            return np.zeros_like(frame)

    notices: list[str] = []
    records: list[dict[str, Any]] = []
    source = Source()
    capture = Capture(
        lambda: engines(RemovedPlayback()), Listening(), records.append, notices.append,
        source=source, backlog_s=10,
    )
    capture.start()
    try:
        await until(capture.ready.is_set)
        source.frames(350, value=1000)
        await until(lambda: any(row.get('captured_through_at') == 3.5 for row in records))
        assert not any('Microphone' in notice for notice in notices)
    finally:
        capture.close()


class GatedEndpointStt(ScriptedStt):
    def __init__(self) -> None:
        super().__init__(words={1000: 'Check', 1500: 'the tests'})
        self.entered = threading.Event()
        self.release = threading.Event()

    def transcribe(self, audio: Samples, *, after: float = 0.0) -> str:
        if not self.heard:
            self.entered.set()
            assert self.release.wait(timeout=3)
        return super().transcribe(audio, after=after)


async def test_queued_continuation_during_stt_keeps_the_opening_and_commits_once() -> None:
    source, stt = Source(), GatedEndpointStt()
    records: list[dict[str, Any]] = []
    heard: list[Heard] = []
    capture = Capture(
        lambda: Engines(CountingEcho(), MarkerWake(), LoudVad(), ScriptedTurn(), stt, Everyone()),
        Listening(), records.append, lambda text: None, source=source, backlog_s=10,
    )
    async def collect() -> None:
        async for event in capture.events():
            if isinstance(event, Heard):
                heard.append(event)
    capture.start()
    collecting = asyncio.create_task(collect())
    try:
        await until(capture.ready.is_set)
        capture.capture(True)
        source.frames(60, value=1000)
        source.frames(60, start=.6)
        assert await asyncio.to_thread(stt.entered.wait, 2)
        source.frames(60, value=1500, start=1.2)
        source.frames(80, start=1.8)
        stt.release.set()
        await until(lambda: any(row.get('captured_through_at') == 2.6 for row in records))
        await asyncio.sleep(.02)
        assert [event.text for event in heard] == ['Check the tests']
        assert len(stt.heard) == 2
        prepared = [row for row in records if row.get('stage') == 'prepared']
        assert len(prepared) == 2
        assert prepared[0]['candidate'] == prepared[1]['candidate']
        assert prepared[0]['endpoint_candidate'] != prepared[1]['endpoint_candidate']
        assert [row['reason'] for row in records if row.get('stage') == 'invalidated'] == ['resumed']
        assert len([row for row in records if row.get('stage') == 'committed']) == 1
        for row in records:
            if row['kind'] == 'endpoint_fence' and row['stage'] == 'processed':
                assert row['processed_frame'] == row['captured_frame_watermark']
    finally:
        stt.release.set()
        capture.close()
        collecting.cancel()
        await asyncio.gather(collecting, return_exceptions=True)


@pytest.mark.parametrize('invalidate', ['capture', 'replacement', 'overflow', 'shutdown'])
async def test_endpoint_ownership_changes_during_stt_reject_the_old_result(invalidate: str) -> None:
    source, stt = Source(), GatedEndpointStt()
    records: list[dict[str, Any]] = []
    events: list[object] = []
    capture = Capture(
        lambda: Engines(CountingEcho(), MarkerWake(), LoudVad(), ScriptedTurn(), stt, Everyone()),
        Listening(), records.append, lambda text: None, source=source, backlog_s=2,
    )
    async def collect() -> None:
        async for event in capture.events():
            events.append(event)
    capture.start()
    collecting = asyncio.create_task(collect())
    try:
        await until(capture.ready.is_set)
        capture.capture(True)
        source.frames(60, value=1000)
        source.frames(60, start=.6)
        assert await asyncio.to_thread(stt.entered.wait, 2)
        if invalidate in ('capture', 'replacement'):
            capture.capture(False)
            if invalidate == 'replacement':
                capture.capture(True)
        elif invalidate == 'overflow':
            source.frames(400, start=1.2)
        else:
            capture.close()
        stt.release.set()
        await until(lambda: any(row.get('stage') == 'invalidated' for row in records))
        await asyncio.sleep(.02)
        assert not any(isinstance(event, Heard) for event in events)
        assert not any(row.get('stage') == 'committed' for row in records)
        assert len(stt.heard) == 1
        assert all('text' not in row for row in records)
    finally:
        stt.release.set()
        capture.close()
        collecting.cancel()
        await asyncio.gather(collecting, return_exceptions=True)


async def test_fixed_fence_commits_while_later_silence_keeps_arriving_without_retranscription() -> None:
    source, stt = Source(), GatedEndpointStt()
    pumping = threading.Event()
    next_at = [3.0]
    class PumpingWake(MarkerWake):
        def score(self, chunk: np.ndarray) -> float:
            if pumping.is_set():
                source.frames(8, start=next_at[0])
                next_at[0] += .08
            return 0.0
    records: list[dict[str, Any]] = []
    heard: list[Heard] = []
    capture = Capture(
        lambda: Engines(CountingEcho(), PumpingWake(), LoudVad(), ScriptedTurn(), stt, Everyone()),
        Listening(), records.append, lambda text: None, source=source, backlog_s=10,
    )
    async def collect() -> None:
        async for event in capture.events():
            if isinstance(event, Heard):
                heard.append(event)
    capture.start()
    collecting = asyncio.create_task(collect())
    try:
        await until(capture.ready.is_set)
        capture.capture(True)
        source.frames(60, value=1000)
        source.frames(60, start=.6)
        assert await asyncio.to_thread(stt.entered.wait, 2)
        source.frames(100, start=1.2)
        pumping.set()
        stt.release.set()
        await until(lambda: bool(heard))
        assert pumping.is_set()
        pumping.clear()
        assert [event.text for event in heard] == ['Check']
        assert len(stt.heard) == 1
        assert len([row for row in records if row['kind'] == 'endpoint_fence' and row['stage'] == 'inserted']) == 1
    finally:
        pumping.clear()
        stt.release.set()
        capture.close()
        collecting.cancel()
        await asyncio.gather(collecting, return_exceptions=True)


@pytest.mark.parametrize('queued_frames,expected', [(1, 'Check'), (4, 'Check the tests')])
async def test_fence_revalidates_only_complete_vad_windows(queued_frames: int, expected: str) -> None:
    class FourFrameVad(LoudVad):
        chunk = 640
    source, stt = Source(), GatedEndpointStt()
    heard: list[Heard] = []
    capture = Capture(
        lambda: Engines(CountingEcho(), MarkerWake(), FourFrameVad(), ScriptedTurn(), stt, Everyone()),
        Listening(), lambda row: None, lambda text: None, source=source, backlog_s=10,
    )
    async def collect() -> None:
        async for event in capture.events():
            if isinstance(event, Heard):
                heard.append(event)
    capture.start()
    collecting = asyncio.create_task(collect())
    try:
        await until(capture.ready.is_set)
        capture.capture(True)
        source.frames(60, value=1000)
        source.frames(52, start=.6)
        assert await asyncio.to_thread(stt.entered.wait, 2)
        source.frames(queued_frames, value=1500, start=1.12)
        stt.release.set()
        if queued_frames == 4:
            await asyncio.sleep(.02)
            assert heard == []
            source.frames(60, start=1.16)
        await until(lambda: bool(heard))
        assert [event.text for event in heard] == [expected]
        assert len(stt.heard) == (2 if queued_frames == 4 else 1)
    finally:
        stt.release.set()
        capture.close()
        collecting.cancel()
        await asyncio.gather(collecting, return_exceptions=True)


@pytest.mark.parametrize('typed_during_stt', [False, True])
@pytest.mark.parametrize('intraturn_wake', [False, True])
async def test_queued_endpoint_delivers_one_complete_hub_request_and_task_and_preserves_typing(
    typed_during_stt: bool, intraturn_wake: bool,
) -> None:
    from jarvis_hub.voice.state import Timing
    from jarvis_hub.voice.voice_mode import VoiceMode, merge
    from tests.fakes import (FakeBackend, FakeClock, FakeTts, RecordingMetrics, RecordingOutput,
                             RecordingPlayer, Text, Tool, Typist, make_hub)

    clock, source, stt = FakeClock(), Source(), GatedEndpointStt()
    spoken = [Text('I will check.'), Tool('delegate', {'request': 'Check the tests', 'context': ''})]
    typed = [Text('Typed input received.')]
    backend = FakeBackend(clock, [typed, spoken] if typed_during_stt else [spoken, typed])
    metrics, output, player = RecordingMetrics(), RecordingOutput(), RecordingPlayer()
    capture = Capture(
        lambda: Engines(CountingEcho(), MarkerWake(), LoudVad(), ScriptedTurn(), stt, Everyone()),
        Listening(), metrics.record, output.notice, source=source, backlog_s=10, clock=clock.now,
    )
    voice = VoiceMode(audio=capture, tts=FakeTts(), player=player, clock=clock,
                      timing=Timing(6.0, 4.0, 6.0, .5), metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=voice.speech, speak_typed=False,
                   on_end_conversation=voice.end_conversation, floor=voice)
    typist = Typist(clock)
    await hub.start()
    capture.start()
    voice.start()
    running = asyncio.create_task(hub.run(merge(typist.lines(), voice.lines(hub.interrupt, hub.woke))))
    try:
        await until(capture.ready.is_set)
        await until(lambda: player.voices == ['ready'])
        source.frames(8, value=2000, start=1000.0)
        await until(lambda: voice.state == 'listening')
        source.frames(220, value=1000, start=1000.08)
        source.frames(60, start=1002.28)
        assert await asyncio.to_thread(stt.entered.wait, 2)
        if typed_during_stt:
            typist.type('Show the status')
            await until(lambda: any('Typed input received.' in text for text in output.replies))
        continuation_at = 1002.88
        if intraturn_wake:
            source.frames(8, value=2000, start=continuation_at)
            continuation_at += .08
        source.frames(60, value=1500, start=continuation_at)
        source.frames(80, start=continuation_at + .6)
        stt.release.set()
        await until(lambda: any(result.name == 'delegate' for session in backend.all_sessions('talker')
                               for result in session.tool_results))
        users = [m for session in backend.all_sessions('talker') for m in session.received if m.startswith('<user')]
        typed_message = '<user lang="en" typed="true">Show the status</user>'
        spoken_message = '<user lang="en">Check the tests</user>'
        assert users == ([typed_message, spoken_message] if typed_during_stt else [spoken_message])
        assert len(backend.all_sessions('orchestrator')) == 1
        assert len([result for session in backend.all_sessions('talker') for result in session.tool_results
                    if result.name == 'delegate']) == 1
        if not typed_during_stt:
            typist.type('Show the status')
        await until(lambda: any('Typed input received.' in text for text in output.replies))
        assert [m for session in backend.all_sessions('talker') for m in session.received
                if m.startswith('<user')] == ([typed_message, spoken_message] if typed_during_stt
                                               else [spoken_message, typed_message])
        assert 'Typed input received.' not in player.texts
    finally:
        stt.release.set()
        typist.end()
        await asyncio.wait([running], timeout=1)
        await hub.close()
        await voice.close()
        capture.close()


async def test_speech_starting_after_the_fence_is_a_later_request() -> None:
    source, stt = Source(), GatedEndpointStt()
    stt.release.set()
    heard: list[Heard] = []
    capture = Capture(
        lambda: Engines(CountingEcho(), MarkerWake(), LoudVad(), ScriptedTurn(), stt, Everyone()),
        Listening(), lambda row: None, lambda text: None, source=source, backlog_s=10,
    )
    async def collect() -> None:
        async for event in capture.events():
            if isinstance(event, Heard):
                heard.append(event)
    capture.start()
    collecting = asyncio.create_task(collect())
    try:
        await until(capture.ready.is_set)
        capture.capture(True)
        source.frames(60, value=1000)
        source.frames(60, start=.6)
        await until(lambda: len(heard) == 1)
        source.frames(60, value=1500, start=1.2)
        source.frames(60, start=1.8)
        await until(lambda: len(heard) == 2)
        assert [event.text for event in heard] == ['Check', 'the tests']
    finally:
        capture.close()
        collecting.cancel()
        await asyncio.gather(collecting, return_exceptions=True)


@pytest.mark.parametrize('fail_call', [1, 2])
async def test_failed_transcription_discards_retained_audio_until_rearmed(fail_call: int) -> None:
    class FailingStt(GatedEndpointStt):
        def transcribe(self, audio: Samples, *, after: float = 0.0) -> str:
            text = super().transcribe(audio, after=after)
            if len(self.heard) == fail_call:
                raise RuntimeError('synthetic STT failure')
            return text
    source, stt = Source(), FailingStt()
    notices: list[str] = []
    heard: list[Heard] = []
    capture = Capture(
        lambda: Engines(CountingEcho(), MarkerWake(), LoudVad(), ScriptedTurn(), stt, Everyone()),
        Listening(), lambda row: None, notices.append, source=source, backlog_s=10,
    )
    async def collect() -> None:
        async for event in capture.events():
            if isinstance(event, Heard):
                heard.append(event)
    capture.start()
    collecting = asyncio.create_task(collect())
    try:
        await until(capture.ready.is_set)
        capture.capture(True)
        source.frames(60, value=1000)
        source.frames(60, start=.6)
        assert await asyncio.to_thread(stt.entered.wait, 2)
        source.frames(60, value=1500, start=1.2)
        source.frames(80, start=1.8)
        stt.release.set()
        await until(lambda: any('synthetic STT failure' in text for text in notices))
        await asyncio.sleep(.02)
        assert heard == []
        assert len(stt.heard) == fail_call
        capture.capture(True)
        source.frames(60, value=1500, start=2.6)
        source.frames(60, start=3.2)
        await until(lambda: bool(heard))
        assert [event.text for event in heard] == ['the tests']
    finally:
        stt.release.set()
        capture.close()
        collecting.cancel()
        await asyncio.gather(collecting, return_exceptions=True)


async def test_repeated_resumption_cannot_grow_retained_audio_without_bound() -> None:
    source = Source()
    class ResumingStt(GatedEndpointStt):
        def __init__(self) -> None:
            super().__init__()
            self.next_at = .8
        def transcribe(self, audio: Samples, *, after: float = 0.0) -> str:
            text = super().transcribe(audio, after=after)
            if len(self.heard) < 12:
                source.frames(40, value=1500, start=self.next_at)
                self.next_at += .4
            return text
    stt = ResumingStt()
    records: list[dict[str, Any]] = []
    capture = Capture(
        lambda: Engines(CountingEcho(), MarkerWake(), LoudVad(), ScriptedTurn(), stt, Everyone()),
        Listening(max_turn_s=.6), records.append, lambda text: None, source=source, backlog_s=2,
    )
    capture.start()
    try:
        await until(capture.ready.is_set)
        capture.capture(True)
        source.frames(80, value=1000)
        assert await asyncio.to_thread(stt.entered.wait, 2)
        stt.release.set()
        await until(lambda: any(row.get('reason') == 'retention_limit' for row in records))
        assert not any(row.get('stage') == 'committed' for row in records)
        assert max(len(audio) for audio in stt.heard) <= 261 * FRAME
    finally:
        stt.release.set()
        capture.close()


@pytest.mark.parametrize('abort', ['inference_failed', 'overflow', 'retention_limit'])
async def test_abandoned_capture_restores_timeout_and_wake_recovery_without_a_request(abort: str) -> None:
    from jarvis_hub.voice.state import Timing
    from jarvis_hub.voice.voice_mode import VoiceMode, merge
    from tests.fakes import (FakeBackend, FakeClock, FakeTts, RecordingMetrics, RecordingOutput,
                             RecordingPlayer, Text, Typist, make_hub)

    clock, source = FakeClock(), Source()
    class AbortingStt(GatedEndpointStt):
        next_at = 1001.28
        resuming = True
        def transcribe(self, audio: Samples, *, after: float = 0.0) -> str:
            text = super().transcribe(audio, after=after)
            if len(self.heard) == 1 and abort == 'inference_failed':
                raise RuntimeError('synthetic STT failure')
            if abort == 'retention_limit' and self.resuming and len(self.heard) < 12:
                source.frames(40, value=1500, start=self.next_at)
                self.next_at += .4
            return text
    stt = AbortingStt()
    backend = FakeBackend(clock, [[Text('Recovered.')]])
    metrics, output, player = RecordingMetrics(), RecordingOutput(), RecordingPlayer()
    capture = Capture(
        lambda: Engines(CountingEcho(), MarkerWake(), LoudVad(), ScriptedTurn(), stt, Everyone()),
        Listening(max_turn_s=.6 if abort == 'retention_limit' else 30), metrics.record, output.notice,
        source=source, backlog_s=2, clock=clock.now,
    )
    voice = VoiceMode(audio=capture, tts=FakeTts(), player=player, clock=clock,
                      timing=Timing(6.0, 4.0, 6.0, .5), metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=voice.speech, speak_typed=False,
                   on_end_conversation=voice.end_conversation, floor=voice)
    typist = Typist(clock)
    await hub.start()
    capture.start()
    voice.start()
    running = asyncio.create_task(hub.run(merge(typist.lines(), voice.lines(hub.interrupt, hub.woke))))
    try:
        await until(capture.ready.is_set)
        await until(lambda: player.voices == ['ready'])
        source.frames(8, value=2000, start=1000)
        await until(lambda: voice.state == 'listening')
        source.frames(60, value=1000, start=1000.08)
        source.frames(60, start=1000.68)
        assert await asyncio.to_thread(stt.entered.wait, 2)
        if abort == 'overflow':
            source.frames(400, value=1500, start=1001.28)
        stt.release.set()
        await until(lambda: any(row.get('reason') == abort for row in metrics.records)
                    or any('synthetic STT failure' in text for text in output.notices))
        for _ in range(30):
            await asyncio.sleep(.001)
        assert not [m for session in backend.all_sessions('talker') for m in session.received if m.startswith('<user')]
        assert backend.all_sessions('orchestrator') == []
        assert not any(player.texts)
        stt.resuming = False
        clock.advance(60)
        await until(lambda: voice.state == 'idle')
        assert any('try again' in text for text in output.notices)
        source.frames(8, value=2000, start=clock.now())
        await until(lambda: voice.state == 'listening')
        source.frames(30, value=1500, start=clock.now() + .08)
        source.frames(80, start=clock.now() + .38)
        await until(lambda: any(m.startswith('<user') for session in backend.all_sessions('talker')
                               for m in session.received))
        assert [m for session in backend.all_sessions('talker') for m in session.received
                if m.startswith('<user')] == ['<user lang="en">the tests</user>']
    finally:
        stt.release.set()
        typist.end()
        await asyncio.wait([running], timeout=1)
        await hub.close()
        await voice.close()
        capture.close()


async def test_failed_old_capture_cannot_abort_its_replacement() -> None:
    from jarvis_hub.voice.state import CaptureAborted

    class FailingFirstStt(GatedEndpointStt):
        def transcribe(self, audio: Samples, *, after: float = 0.0) -> str:
            text = super().transcribe(audio, after=after)
            if len(self.heard) == 1:
                raise RuntimeError('old capture failed')
            return text
    source, stt = Source(), FailingFirstStt()
    events: list[object] = []
    capture = Capture(
        lambda: Engines(CountingEcho(), MarkerWake(), LoudVad(), ScriptedTurn(), stt, Everyone()),
        Listening(), lambda row: None, lambda text: None, source=source, backlog_s=10,
    )
    async def collect() -> None:
        async for event in capture.events():
            events.append(event)
    capture.start()
    collecting = asyncio.create_task(collect())
    try:
        await until(capture.ready.is_set)
        capture.capture(True)
        source.frames(60, value=1000)
        source.frames(60, start=.6)
        assert await asyncio.to_thread(stt.entered.wait, 2)
        capture.capture(False)
        capture.capture(True)
        source.frames(60, value=1500, start=1.2)
        source.frames(80, start=1.8)
        stt.release.set()
        await until(lambda: any(isinstance(event, Heard) for event in events))
        assert not any(isinstance(event, CaptureAborted) for event in events)
        assert [event.text for event in events if isinstance(event, Heard)] == ['the tests']
    finally:
        stt.release.set()
        capture.close()
        collecting.cancel()
        await asyncio.gather(collecting, return_exceptions=True)
