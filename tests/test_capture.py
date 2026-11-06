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
from jarvis_hub.voice.idle_wake import WakeMatch
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


class DelayedIdleWake:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed = False
        self.finished = threading.Event()
        self.phrase_only = False

    def recognizes(self, audio: Samples) -> WakeMatch:
        self.entered.set()
        self.release.wait(timeout=5)
        self.finished.set()
        return WakeMatch(bool((audio == 1000 / 32768).any()), self.phrase_only)

    def close(self) -> None:
        self.closed = True
        self.release.set()


@pytest.mark.parametrize("wake_only_then_continued", [False, True])
async def test_delayed_idle_confirmation_transfers_the_complete_utterance_through_ordinary_stt(wake_only_then_continued: bool) -> None:
    from jarvis_hub.voice.state import WakeWord

    source, wake, echo = Source(), DelayedIdleWake(), CountingEcho()
    wake.phrase_only = wake_only_then_continued
    stt = ScriptedStt(words={1000: "Jarvis", 1500: "check every result"})
    capture = Capture(
        lambda: Engines(echo, MarkerWake(), LoudVad(), ScriptedTurn(), stt, Everyone(), idle_wake=wake),
        Listening(), lambda row: None, lambda text: None, source=source, backlog_s=10,
    )
    received: list[Any] = []

    async def collect() -> None:
        async for event in capture.events():
            received.append(event)
            if isinstance(event, WakeWord):
                capture.capture(True, owner=1, idle=False, wake_candidate=event.idle_candidate)

    capture.start()
    collecting = asyncio.create_task(collect())
    try:
        await until(capture.ready.is_set)
        capture.capture(False, owner=0, idle=True)
        source.frames(80, value=1000)
        if wake_only_then_continued:
            source.frames(60, start=.8)
        else:
            source.frames(320, value=1500, start=.8)
            source.frames(60, start=4)
        assert await asyncio.to_thread(wake.entered.wait, 2)
        if wake_only_then_continued:
            source.frames(320, value=1500, start=1.4)
            source.frames(360, start=4.6)
        else:
            source.frames(300, start=4.6)
        capture.reference(np.zeros(240, np.float32), 24000)
        assert echo.referenced == 240
        assert received == []
        wake.release.set()
        await until(lambda: any(isinstance(event, Heard) for event in received))

        heard = [event for event in received if isinstance(event, Heard)]
        assert [event.text for event in heard] == ["Jarvis check every result"]
        assert heard[0].owner == 1
        assert len([event for event in received if isinstance(event, WakeWord)]) == 1
        assert stt.after == [0.0]
        assert len(stt.heard) == 1
        assert (stt.heard[0][:80 * FRAME] == 1000 / 32768).all()
    finally:
        wake.release.set()
        capture.close()
        collecting.cancel()
        await asyncio.gather(collecting, return_exceptions=True)


@pytest.mark.parametrize("replacement", ["owner", "shutdown"])
async def test_idle_wake_handoff_rejects_a_proposal_after_its_source_owner_changes(replacement: str) -> None:
    from jarvis_hub.voice.state import WakeWord

    source, wake = Source(), DelayedIdleWake()
    stt = ScriptedStt()
    capture = Capture(
        lambda: Engines(CountingEcho(), MarkerWake(), LoudVad(), ScriptedTurn(), stt, Everyone(), idle_wake=wake),
        Listening(), lambda row: None, lambda text: None, source=source, backlog_s=10,
    )
    capture.start()
    try:
        await until(capture.ready.is_set)
        capture.capture(False, owner=0, idle=True)
        source.frames(80, value=1000)
        source.frames(60, start=.8)
        assert await asyncio.to_thread(wake.entered.wait, 2)
        wake.release.set()
        event = await asyncio.wait_for(anext(capture.events()), 1)
        assert isinstance(event, WakeWord)
        if replacement == "shutdown":
            capture.close()
        else:
            capture.capture(False, owner=2, idle=True)

        accepted = capture.capture(True, owner=1, wake_candidate=event.idle_candidate)

        assert accepted is False
        assert stt.heard == []
    finally:
        capture.close()


@pytest.mark.parametrize("replacement", ["typed", "playback", "loss", "shutdown"])
async def test_pending_idle_recognition_has_no_authority_after_invalidation(replacement: str) -> None:
    source, wake = Source(), DelayedIdleWake()
    rows: list[dict[str, Any]] = []
    stt = ScriptedStt()
    capture = Capture(
        lambda: Engines(CountingEcho(), MarkerWake(), LoudVad(), ScriptedTurn(), stt, Everyone(), idle_wake=wake),
        Listening(), rows.append, lambda text: None, source=source, backlog_s=2,
    )
    received: list[Any] = []

    async def collect() -> None:
        async for event in capture.events():
            received.append(event)

    capture.start()
    collecting = asyncio.create_task(collect())
    try:
        await until(capture.ready.is_set)
        capture.capture(False, owner=0, idle=True)
        source.frames(80, value=1000)
        source.frames(60, start=.8)
        assert await asyncio.to_thread(wake.entered.wait, 2)
        if replacement == "shutdown":
            capture.close()
        elif replacement == "loss":
            source.frames(1000, start=1.4)
            await until(lambda: any(row.get("reason") == "overflow" for row in rows))
        else:
            capture.capture(False, owner=1 if replacement == "typed" else 0, idle=replacement == "typed")
            await until(lambda: any(row.get("reason") == "mode_changed" for row in rows))
        wake.release.set()
        assert await asyncio.to_thread(wake.finished.wait, 2)
        if replacement != "shutdown":
            await until(lambda: any(row.get("stage") == "stale_result" for row in rows))
        assert received == []
        assert stt.heard == []
        if replacement == "loss":
            source.frames(80, value=1000, start=11.4)
            source.frames(60, start=12.2)
            await until(lambda: bool(received))
            assert capture.capture(True, owner=1, wake_candidate=received[0].idle_candidate)
            await until(lambda: len(stt.heard) == 1)
    finally:
        capture.close()
        collecting.cancel()
        await asyncio.gather(collecting, return_exceptions=True)


@pytest.mark.parametrize("phrase_only", [False, True])
async def test_delayed_idle_wake_through_the_hub_handles_one_breath_or_waits_without_a_turn(phrase_only: bool) -> None:
    from jarvis_hub.voice.state import Timing
    from jarvis_hub.voice.voice_mode import VoiceMode, merge
    from tests.fakes import (FakeBackend, FakeClock, FakeTts, RecordingMetrics, RecordingOutput,
                             RecordingPlayer, Text, Typist, make_hub)

    clock, source, wake = FakeClock(), Source(), DelayedIdleWake()
    wake.phrase_only = phrase_only
    stt = ScriptedStt(text="A Jarvis" if phrase_only else "Hey Jarvis, inspect the current CI status.")
    backend = FakeBackend(clock, [] if phrase_only else [[Text("The checks passed, sir.")]])
    metrics, output, player = RecordingMetrics(), RecordingOutput(), RecordingPlayer()
    capture = Capture(
        lambda: Engines(CountingEcho(), MarkerWake(), LoudVad(), ScriptedTurn(), stt, Everyone(), idle_wake=wake),
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
        await until(lambda: player.voices == ["ready"])
        began = clock.now()
        source.frames(400, value=1000, start=began)
        source.frames(60, start=began + 4)
        assert await asyncio.to_thread(wake.entered.wait, 2)
        clock.advance(12)
        source.frames(300, start=began + 4.6)
        wake.release.set()
        if phrase_only:
            await until(lambda: voice.state == "listening" and player.voices.count("chime") == 1)
            clock.advance(5.9)
            for _ in range(30):
                await asyncio.sleep(0)
            assert voice.state == "listening"
        else:
            await until(lambda: len(stt.heard) == 1)
            await until(lambda: any(row.get("stage") == "committed" for row in metrics.records))
            await until(lambda: voice.state == "follow_up")
        messages = [m for session in backend.all_sessions("talker") for m in session.received if m.startswith("<user")]
        assert messages == ([] if phrase_only else ['<user lang="en">inspect the current CI status.</user>'])
        assert player.voices.count("chime") == 1
        assert stt.after == ([] if phrase_only else [0.0])
        assert backend.all_sessions("orchestrator") == []
    finally:
        wake.release.set()
        typist.end()
        await asyncio.wait([running], timeout=1)
        await hub.close()
        await voice.close()
        capture.close()


@pytest.mark.parametrize("bound", ["duration_limit", "retention_limit", "queue_limit"])
async def test_idle_wake_limits_reject_audio_instead_of_admitting_a_partial_request(bound: str) -> None:
    source, wake = Source(), DelayedIdleWake()
    rows: list[dict[str, Any]] = []
    stt = ScriptedStt()
    capture = Capture(
        lambda: Engines(CountingEcho(), None, LoudVad(), ScriptedTurn(), stt, Everyone(), idle_wake=wake),
        Listening(), rows.append, lambda text: None, source=source, backlog_s=60,
    )
    capture.start()
    try:
        await until(capture.ready.is_set)
        capture.capture(False, owner=0, idle=True)
        if bound == "duration_limit":
            source.frames(3100, value=1000)
        else:
            source.frames(80, value=1000)
            source.frames(60, start=.8)
            assert await asyncio.to_thread(wake.entered.wait, 2)
            if bound == "retention_limit":
                source.frames(4300, start=1.4)
            else:
                for i in range(3):
                    source.frames(80, value=1000, start=1.4 * (i + 1))
                    source.frames(60, start=1.4 * (i + 1) + .8)
        await until(lambda: any(row.get("reason") == bound for row in rows), timeout=3)
        assert stt.heard == []
        if bound == "duration_limit":
            assert not wake.entered.is_set()
    finally:
        capture.close()


async def test_failed_idle_recognition_reports_a_repair_without_admitting_audio() -> None:
    class FailedWake(DelayedIdleWake):
        def recognizes(self, audio: Samples) -> WakeMatch:
            raise RuntimeError("synthetic native failure")

    source, wake, notices = Source(), FailedWake(), list[str]()
    stt = ScriptedStt()
    capture = Capture(
        lambda: Engines(CountingEcho(), None, LoudVad(), ScriptedTurn(), stt, Everyone(), idle_wake=wake),
        Listening(), lambda row: None, notices.append, source=source, backlog_s=10,
    )
    capture.start()
    try:
        await until(capture.ready.is_set)
        source.frames(80, value=1000)
        source.frames(60, start=.8)
        await until(lambda: any("apple_wake --check" in text for text in notices))
        assert wake.closed
        assert stt.heard == []
    finally:
        capture.close()


async def test_queued_wake_confirmation_cannot_revoke_the_first_unclaimed_activation() -> None:
    from jarvis_hub.voice.state import WakeWord

    source, wake = Source(), DelayedIdleWake()
    rows: list[dict[str, Any]] = []
    capture = Capture(
        lambda: Engines(CountingEcho(), None, LoudVad(), ScriptedTurn(), ScriptedStt(), Everyone(), idle_wake=wake),
        Listening(), rows.append, lambda text: None, source=source, backlog_s=10,
    )
    capture.start()
    try:
        await until(capture.ready.is_set)
        capture.capture(False, owner=0, idle=True)
        source.frames(80, value=1000)
        source.frames(60, start=.8)
        assert await asyncio.to_thread(wake.entered.wait, 2)
        source.frames(80, value=1000, start=1.4)
        source.frames(60, start=2.2)
        await until(lambda: sum(row.get("stage") == "submitted" for row in rows) == 2)
        wake.release.set()
        await until(lambda: sum(row.get("stage") == "keyword_matched" for row in rows) == 2)
        source.frames(80, value=1000, start=2.8)
        source.frames(60, start=3.6)
        await until(lambda: any(row.get("reason") == "queue_limit" for row in rows))
        assert sum(row.get("stage") == "submitted" for row in rows) == 2
        first = await asyncio.wait_for(anext(capture.events()), 1)
        assert isinstance(first, WakeWord)

        assert capture.capture(True, owner=1, wake_candidate=first.idle_candidate)
    finally:
        capture.close()


async def test_audio_loss_after_wake_acknowledgment_aborts_the_new_owner_before_partial_input() -> None:
    from jarvis_hub.voice.state import CaptureAborted, WakeWord

    class PausedVad(LoudVad):
        def __init__(self) -> None:
            self.pause = False
            self.entered, self.release = threading.Event(), threading.Event()

        def probability(self, chunk: np.ndarray) -> float:
            if self.pause:
                self.pause = False
                self.entered.set()
                assert self.release.wait(timeout=3)
            return super().probability(chunk)

    source, wake, vad = Source(), DelayedIdleWake(), PausedVad()
    records, notices, received = list[dict[str, Any]](), list[str](), list[Any]()
    stt = ScriptedStt()
    capture = Capture(
        lambda: Engines(CountingEcho(), None, vad, ScriptedTurn(), stt, Everyone(), idle_wake=wake),
        Listening(), records.append, notices.append, source=source, backlog_s=5,
    )

    async def collect() -> None:
        async for event in capture.events():
            received.append(event)

    capture.start()
    collecting = asyncio.create_task(collect())
    try:
        await until(capture.ready.is_set)
        capture.capture(False, owner=0, idle=True)
        source.frames(80, value=1000)
        source.frames(320, value=1500, start=.8)
        source.frames(60, start=4)
        assert await asyncio.to_thread(wake.entered.wait, 2)
        wake.release.set()
        await until(lambda: any(isinstance(event, WakeWord) for event in received))
        proposal = next(event for event in received if isinstance(event, WakeWord))
        vad.pause = True
        source.frames(4, start=4.6)
        assert await asyncio.to_thread(vad.entered.wait, 2)
        assert capture.capture(True, owner=1, wake_candidate=proposal.idle_candidate)
        source.frames(1000, start=4.64)
        await until(lambda: any('dropping audio' in text for text in notices))
        vad.release.set()

        await until(lambda: any(isinstance(event, CaptureAborted) for event in received))
        aborted = next(event for event in received if isinstance(event, CaptureAborted))
        assert aborted.owner == 1 and aborted.reason == 'overflow' and aborted.wake_handoff
        source.frames(80, value=1000, start=14.64)
        source.frames(60, start=15.44)
        await until(lambda: any(row.get('captured_through_at', 0) >= 16.03 for row in records))
        assert not any(isinstance(event, Heard) for event in received)

        capture.capture(False, owner=2, idle=True)
        source.frames(80, value=1000, start=16.04)
        source.frames(60, start=16.84)
        await until(lambda: sum(isinstance(event, WakeWord) for event in received) == 2)
        retry = [event for event in received if isinstance(event, WakeWord)][-1]
        assert capture.capture(True, owner=3, wake_candidate=retry.idle_candidate)
        await until(lambda: any(isinstance(event, Heard) for event in received))
        assert [event.owner for event in received if isinstance(event, Heard)] == [3]
        assert len(stt.heard) == 1
    finally:
        vad.release.set()
        capture.close()
        collecting.cancel()
        await asyncio.gather(collecting, return_exceptions=True)


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
@pytest.mark.parametrize('one_breath', [False, True])
async def test_queued_endpoint_delivers_one_complete_hub_request_and_task_and_preserves_typing(
    typed_during_stt: bool, intraturn_wake: bool, one_breath: bool,
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
        if one_breath:
            source.frames(140, value=1000, start=1000.08)
        await until(lambda: voice.state == 'listening')
        if not one_breath:
            source.frames(140, value=1000, start=1000.08)
        source.frames(60, start=1001.48)
        assert await asyncio.to_thread(stt.entered.wait, 2)
        if typed_during_stt:
            typist.type('Show the status')
            await until(lambda: any('Typed input received.' in text for text in output.replies))
        continuation_at = 1002.08
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
        Listening(max_turn_s=1.2 if abort == 'retention_limit' else 30), metrics.record, output.notice,
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


async def test_stale_vad_start_cannot_take_ownership_of_replacement_listening() -> None:
    from collections.abc import AsyncIterator
    from jarvis_hub.voice.state import InputEvent, Timing, WakeWord
    from jarvis_hub.voice.voice_mode import VoiceMode, merge
    from tests.fakes import (FakeBackend, FakeClock, FakeTts, RecordingMetrics, RecordingOutput,
                             RecordingPlayer, Typist, make_hub)

    class GatedVad(LoudVad):
        def __init__(self) -> None:
            self.entered, self.release = threading.Event(), threading.Event()
            self.once = True
        def probability(self, chunk: np.ndarray) -> float:
            if self.once and (chunk == 1000).any():
                self.once = False
                self.entered.set()
                assert self.release.wait(timeout=3)
            return super().probability(chunk)

    clock, source, vad = FakeClock(), Source(), GatedVad()
    backend = FakeBackend(clock)
    metrics, output, player = RecordingMetrics(), RecordingOutput(), RecordingPlayer()
    capture = Capture(
        lambda: Engines(CountingEcho(), MarkerWake(), vad, ScriptedTurn(), ScriptedStt(), Everyone()),
        Listening(), metrics.record, output.notice, source=source, backlog_s=10, clock=clock.now,
    )
    class InterleavedWake:
        def __init__(self) -> None:
            self.queue: asyncio.Queue[InputEvent] = asyncio.Queue()
        def wake(self) -> None:
            self.queue.put_nowait(WakeWord(clock.now()))
        def capture(self, on: bool, *, owner: int | None = None, idle: bool = False, wake_candidate: int | None = None) -> bool:
            return capture.capture(on, owner=owner, idle=idle, wake_candidate=wake_candidate)
        def barge_in(self, since: float | None) -> None:
            capture.barge_in(since)
        async def events(self) -> AsyncIterator[InputEvent]:
            async def forward() -> None:
                async for event in capture.events():
                    self.queue.put_nowait(event)
            forwarding = asyncio.create_task(forward())
            try:
                while True:
                    yield await self.queue.get()
            finally:
                forwarding.cancel()
                await asyncio.gather(forwarding, return_exceptions=True)
    audio = InterleavedWake()
    voice = VoiceMode(audio=audio, tts=FakeTts(), player=player, clock=clock,
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
        audio.wake()
        await until(lambda: voice.state == 'listening')
        source.frames(4, value=1000, start=clock.now())
        assert await asyncio.to_thread(vad.entered.wait, 2)
        clock.advance(7)
        await until(lambda: voice.state == 'idle')
        audio.wake()
        await until(lambda: voice.state == 'listening')
        vad.release.set()
        for _ in range(30):
            await asyncio.sleep(.001)
        source.frames(8, start=clock.now())
        for _ in range(30):
            await asyncio.sleep(.001)
        clock.advance(60)
        await until(lambda: voice.state == 'idle')
        assert not [m for session in backend.all_sessions('talker') for m in session.received if m.startswith('<user')]
        assert backend.all_sessions('orchestrator') == []
        assert not any(player.texts)
    finally:
        vad.release.set()
        typist.end()
        await asyncio.wait([running], timeout=1)
        await hub.close()
        await voice.close()
        capture.close()


@pytest.mark.parametrize('disable_first', [False, True])
async def test_replacement_capture_cannot_recreate_a_discarded_onset(disable_first: bool) -> None:
    from jarvis_hub.voice.state import SpeechStarted

    class GatedVad(LoudVad):
        def __init__(self) -> None:
            self.entered, self.release = threading.Event(), threading.Event()
            self.once = True
        def probability(self, chunk: np.ndarray) -> float:
            if self.once:
                self.once = False
                self.entered.set()
                assert self.release.wait(timeout=3)
            return super().probability(chunk)
    source, vad = Source(), GatedVad()
    stt = ScriptedStt(words={1000: 'Check', 1500: 'the tests'})
    records: list[dict[str, Any]] = []
    events: list[object] = []
    capture = Capture(
        lambda: Engines(CountingEcho(), MarkerWake(), vad, ScriptedTurn(), stt, Everyone()),
        Listening(), records.append, lambda text: None, source=source, backlog_s=10,
    )
    async def collect() -> None:
        async for event in capture.events():
            events.append(event)
    capture.start()
    collecting = asyncio.create_task(collect())
    try:
        await until(capture.ready.is_set)
        capture.capture(True)
        source.frames(4, value=1000)
        assert await asyncio.to_thread(vad.entered.wait, 2)
        if disable_first:
            capture.capture(False)
        capture.capture(True)
        source.frames(60, start=.04)
        vad.release.set()
        await until(lambda: any(row.get('captured_through_at') == .64 for row in records))
        await asyncio.sleep(.02)
        assert not any(isinstance(event, (SpeechStarted, Heard)) for event in events)
        assert stt.heard == []
        source.frames(60, value=1500, start=.64)
        source.frames(60, start=1.24)
        await until(lambda: any(isinstance(event, Heard) for event in events))
        assert [event.text for event in events if isinstance(event, Heard)] == ['the tests']
    finally:
        vad.release.set()
        capture.close()
        collecting.cancel()
        await asyncio.gather(collecting, return_exceptions=True)


async def test_same_frame_wake_survives_old_vad_without_releasing_its_stale_speech_start() -> None:
    from jarvis_hub.voice.state import Timing
    from jarvis_hub.voice.voice_mode import VoiceMode, merge
    from tests.fakes import (FakeBackend, FakeClock, FakeTts, RecordingMetrics, RecordingOutput,
                             RecordingPlayer, Text, Typist, make_hub)

    class GatedWakeVad(LoudVad):
        chunk = 1280
        def __init__(self) -> None:
            self.armed = False
            self.entered, self.release = threading.Event(), threading.Event()
        def probability(self, chunk: np.ndarray) -> float:
            if self.armed and (chunk == 2000).any():
                self.armed = False
                self.entered.set()
                assert self.release.wait(timeout=3)
            return super().probability(chunk)
    clock, source, vad = FakeClock(), Source(), GatedWakeVad()
    stt = ScriptedStt(words={1000: 'Check', 1500: 'the tests'})
    backend = FakeBackend(clock, [[Text('Recovered.')]])
    metrics, output, player = RecordingMetrics(), RecordingOutput(), RecordingPlayer()
    capture = Capture(
        lambda: Engines(CountingEcho(), MarkerWake(), vad, ScriptedTurn(), stt, Everyone()),
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
        source.frames(8, value=2000, start=clock.now())
        source.frames(96, start=clock.now() + .08)
        await until(lambda: voice.state == 'listening')
        clock.advance(3)
        vad.armed = True
        source.frames(8, value=2000, start=clock.now())
        assert await asyncio.to_thread(vad.entered.wait, 2)
        clock.advance(4)
        await until(lambda: voice.state == 'idle')
        vad.release.set()
        await until(lambda: voice.state == 'listening')
        source.frames(8, start=clock.now())
        for _ in range(30):
            await asyncio.sleep(.001)
        assert stt.heard == []
        assert not [m for session in backend.all_sessions('talker') for m in session.received if m.startswith('<user')]
        assert backend.all_sessions('orchestrator') == []
        clock.advance(60)
        await until(lambda: voice.state == 'idle')
        source.frames(8, value=2000, start=clock.now())
        source.frames(30, value=1000, start=clock.now() + .08)
        await until(lambda: voice.state == 'listening')
        source.frames(30, value=1500, start=clock.now() + .38)
        source.frames(60, start=clock.now() + .68)
        await until(lambda: any(m.startswith('<user') for session in backend.all_sessions('talker')
                               for m in session.received))
        assert [m for session in backend.all_sessions('talker') for m in session.received
                if m.startswith('<user')] == ['<user lang="en">Check the tests</user>']
    finally:
        vad.release.set()
        typist.end()
        await asyncio.wait([running], timeout=1)
        await hub.close()
        await voice.close()
        capture.close()


@pytest.mark.parametrize('reply_finished', [False, True])
async def test_same_owner_reply_phase_off_preserves_the_existing_endpoint_fence(reply_finished: bool) -> None:
    source, stt = Source(), GatedEndpointStt()
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
        capture.capture(True, owner=7)
        source.frames(60, value=1000)
        source.frames(60, start=.6)
        assert await asyncio.to_thread(stt.entered.wait, 2)
        capture.capture(False, owner=7)
        if reply_finished:
            capture.capture(True, owner=7)
        source.frames(60, value=1500, start=1.2)
        source.frames(80, start=1.8)
        stt.release.set()
        await until(lambda: bool(heard))
        assert [event.text for event in heard] == ['Check the tests']
        assert heard[0].owner == 7
        assert heard[0].candidate is not None
        assert heard[0].started_at is not None and heard[0].started_at < .1
        if reply_finished:
            return
        source.frames(60, value=1500, start=2.6)
        source.frames(80, start=3.2)
        for _ in range(20):
            await asyncio.sleep(.001)
        assert len(heard) == 1
    finally:
        stt.release.set()
        capture.close()
        collecting.cancel()
        await asyncio.gather(collecting, return_exceptions=True)


async def test_replacement_after_reply_phase_off_drops_the_old_retained_turn() -> None:
    source, stt = Source(), GatedEndpointStt()
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
        capture.capture(True, owner=7)
        source.frames(60, value=1000)
        source.frames(60, start=.6)
        assert await asyncio.to_thread(stt.entered.wait, 2)
        capture.capture(False, owner=7)
        capture.capture(True, owner=8)
        source.frames(60, value=1500, start=1.2)
        source.frames(80, start=1.8)
        stt.release.set()
        await until(lambda: bool(heard))
        assert [(event.text, event.owner) for event in heard] == [('the tests', 8)]
    finally:
        stt.release.set()
        capture.close()
        collecting.cancel()
        await asyncio.gather(collecting, return_exceptions=True)


async def test_full_playback_candidate_keeps_opening_beyond_preroll_with_reference_active() -> None:
    source, echo = Source(), CountingEcho()
    stt = ScriptedStt(words={1000: 'Actually only review', 3000: 'the endpoint changes'})
    capture = Capture(
        lambda: Engines(echo, MarkerWake(), LoudVad(), ScriptedTurn(), stt, Everyone()),
        Listening(preroll_s=.3), lambda row: None, lambda notice: None, source=source, backlog_s=6,
    )
    received: list[Any] = []

    async def collect() -> None:
        async for event in capture.events():
            received.append(event)

    capture.start()
    collecting = asyncio.create_task(collect())
    try:
        await until(capture.ready.is_set)
        capture.capture(True, owner=7, idle=False)
        capture.barge_in(None)
        capture.reference(np.ones(2400, np.float32), 24000)
        source.frames(100, value=1000)
        source.frames(300, value=3000, start=1)
        source.frames(60, start=4)
        await until(lambda: any(isinstance(event, Heard) for event in received))
        heard = [event for event in received if isinstance(event, Heard)]
        assert len(heard) == 1
        assert heard[0].text == 'Actually only review the endpoint changes'
        assert heard[0].owner == 7
        assert echo.referenced == 2400 and len(stt.heard) == 1
        assert len(stt.heard[0]) > 3 * 16000
    finally:
        capture.close()
        collecting.cancel()
        await asyncio.gather(collecting, return_exceptions=True)


async def test_nonidle_wake_hit_cannot_cut_the_opening_from_ordinary_stt() -> None:
    class LateVad(LoudVad):
        def __init__(self) -> None:
            self.calls = 0

        def probability(self, chunk: np.ndarray) -> float:
            self.calls += 1
            return 0.0 if self.calls <= 3 else super().probability(chunk)

    source = Source()
    stt = ScriptedStt(words={2000: 'Jarvis was quoted', 3000: 'by someone else'})
    capture = Capture(
        lambda: Engines(CountingEcho(), MarkerWake(), LateVad(), ScriptedTurn(), stt, Everyone()),
        Listening(), lambda row: None, lambda notice: None, source=source, backlog_s=2,
    )
    received: list[Any] = []
    async def collect() -> None:
        async for event in capture.events():
            received.append(event)
    capture.start()
    collecting = asyncio.create_task(collect())
    try:
        await until(capture.ready.is_set)
        capture.capture(True, owner=9, idle=False)
        capture.barge_in(None)
        source.frames(16, value=2000)
        source.frames(40, value=3000, start=.16)
        source.frames(60, start=.56)
        await until(lambda: any(isinstance(event, Heard) for event in received))
        assert stt.after == [0.0]
        assert [event.text for event in received if isinstance(event, Heard)] == ['Jarvis was quoted by someone else']
    finally:
        capture.close()
        collecting.cancel()
        await asyncio.gather(collecting, return_exceptions=True)
