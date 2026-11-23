"""Live mode at the loopback Door: a paired iPhone streams its microphone into the real voice floor.

The phone's Capture runs with marker-sample engines on its own thread; the Mac's input is scripted.
"""

import asyncio
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import numpy as np

from jarvis_hub.door import Door
from jarvis_hub.hub import Hub
from jarvis_hub.pairing import PairingResult, PairingStore
from jarvis_hub.voice.engines import NoEchoCancellation
from jarvis_hub.voice.input import Engines, Listening
from jarvis_hub.voice.live import LiveInputs
from jarvis_hub.voice.state import Timing
from jarvis_hub.voice.voice_mode import VoiceMode
from tests.fakes import FakeBackend, FakeClock, FakeTts, RecordingMetrics, RecordingPlayer, ScriptedAudio, Text, until
from tests.protocol_exchanges import connect
from tests.test_input_pipeline import SPEECH, WAKE, Everyone, LoudVad, MarkerWake, ScriptedStt, ScriptedTurn
from tests.test_remote_speech import control, foreground, remote_rig

FRAME = 160
READY = 'Listening ready: waiting for "Hey Jarvis".'


def pcm(frames: int, value: int = 0) -> bytes:
    return np.full(frames * FRAME, value, "<i2").tobytes()


@dataclass
class Rig:
    session: aiohttp.ClientSession
    door: Door
    hub: Hub
    voice: VoiceMode
    mac: ScriptedAudio
    backend: FakeBackend
    phones: list[PairingResult]
    pairing: PairingStore
    metrics: RecordingMetrics
    notices: list[str]
    # The threads the phone Captures loaded their engines on, one per Live mode turned on.
    threads: list[threading.Thread] = field(default_factory=lambda: [])

    def requests(self) -> list[str]:
        return [m for s in self.backend.all_sessions("talker") for m in s.received if m.startswith("<user")]

    def ready(self) -> int:
        return self.notices.count(READY)


@asynccontextmanager
async def live_rig(tmp_path: Path) -> AsyncGenerator[Rig]:
    clock, metrics, notices = FakeClock(), RecordingMetrics(), list[str]()
    mac = ScriptedAudio(clock)
    voice = VoiceMode(audio=mac, tts=FakeTts(), player=RecordingPlayer(), clock=clock, metrics=metrics,
                      notice=lambda text: None, timing=Timing(6, 4, 6, .5, tail_s=0))
    threads: list[threading.Thread] = []

    def load() -> Engines:
        threads.append(threading.current_thread())
        return Engines(echo=NoEchoCancellation(), wake=MarkerWake(), vad=LoudVad(), turn=ScriptedTurn(),
                       stt=ScriptedStt(), gate=Everyone())

    def live(floor: VoiceMode) -> LiveInputs:
        return LiveInputs(floor, load, Listening(), metrics.record, notices.append, clock=clock.now)

    backend = FakeBackend(clock, [[Text("Opened.")], [Text("Second.")]])
    async with remote_rig(tmp_path, backend=backend, voice=voice, live=live) as (session, door, hub, _log, _backend, phones, pairing):
        running = asyncio.create_task(hub.run(voice.lines(hub.interrupt, hub.woke, hub.admit_spoken_revision)))
        try:
            yield Rig(session, door, hub, voice, mac, backend, phones, pairing, metrics, notices, threads)
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


async def live_on(rig: Rig, socket: aiohttp.ClientWebSocketResponse, id: str = "live-on") -> None:
    loaded = rig.ready()
    assert (await control(socket, id, "set_live", on=True))["result"] == {"on": True}
    await until(lambda: rig.ready() > loaded)


async def stream(socket: aiohttp.ClientWebSocketResponse, frames: int, value: int = 0) -> None:
    for _ in range(frames // 10):
        await socket.send_bytes(pcm(10, value))


async def test_a_wake_word_said_to_the_phone_opens_listening_and_its_request_reaches_the_talker(tmp_path: Path) -> None:
    async with live_rig(tmp_path) as rig, connect(rig.session, rig.door, rig.phones[0].token) as socket:
        await foreground(socket)
        await live_on(rig, socket)

        await socket.send_bytes(pcm(8, WAKE))
        await until(lambda: rig.voice.state == "listening")
        await stream(socket, 80, SPEECH)
        await stream(socket, 100)

        await until(lambda: any("Open a PR" in m for m in rig.requests()))
        phone = rig.phones[0].client.id
        assert {r["client"] for r in rig.metrics.records if r["kind"] == "turn"} == {phone}
        assert [r["client"] for r in rig.metrics.records if r["kind"] == "voice"][0] == phone


async def test_set_live_answers_on_and_a_retry_returns_the_original_result(tmp_path: Path) -> None:
    async with live_rig(tmp_path) as rig, connect(rig.session, rig.door, rig.phones[0].token) as socket:
        await foreground(socket)
        await live_on(rig, socket, "same")
        assert (await control(socket, "off", "set_live", on=False))["result"] == {"on": False}

        retried = await control(socket, "same", "set_live", on=True)

        assert retried["ok"] and retried["result"] == {"on": True}
        await socket.send_bytes(pcm(10))
        closed = await socket.receive(timeout=2)
        assert closed.type == aiohttp.WSMsgType.CLOSE and closed.data == 1002


async def test_set_live_refuses_a_bad_payload_and_a_client_that_is_not_an_iphone(tmp_path: Path) -> None:
    async with live_rig(tmp_path) as rig:
        reference = rig.pairing.exchange(rig.pairing.create_code("https://mac.example.ts.net"), "Bench", "reference")
        async with connect(rig.session, rig.door, rig.phones[0].token) as phone, \
                connect(rig.session, rig.door, reference.token) as bench:
            await foreground(phone)
            await foreground(bench)
            for id, payload in (("string", {"on": "yes"}), ("empty", {}), ("extra", {"on": True, "mic": 1})):
                result = await control(phone, id, "set_live", **payload)
                assert result["error"]["code"] == "invalid_payload"
            assert (await control(bench, "bench", "set_live", on=True))["error"]["code"] == "invalid_payload"
            assert rig.ready() == 0


async def test_set_live_without_voice_mode_is_voice_unavailable(tmp_path: Path) -> None:
    async with remote_rig(tmp_path) as (session, door, _hub, _log, _backend, phones, _pairing), \
            connect(session, door, phones[0].token) as socket:
        await foreground(socket)

        result = await control(socket, "live", "set_live", on=True)

        assert not result["ok"] and result["error"]["code"] == "voice_unavailable"


async def closes_with(socket: aiohttp.ClientWebSocketResponse, data: bytes) -> int | None:
    await socket.send_bytes(data)
    while True:
        message = await socket.receive(timeout=2)
        if message.type == aiohttp.WSMsgType.CLOSE:
            return message.data
        if message.type == aiohttp.WSMsgType.CLOSED:
            return socket.close_code


async def test_uplink_audio_while_live_mode_is_off_or_of_the_wrong_length_closes_the_socket(tmp_path: Path) -> None:
    async with live_rig(tmp_path) as rig:
        async with connect(rig.session, rig.door, rig.phones[0].token) as socket:
            await foreground(socket)
            assert await closes_with(socket, pcm(10)) == 1002
        for wrong in (pcm(1)[:-2], pcm(11), b""):
            async with connect(rig.session, rig.door, rig.phones[0].token) as socket:
                await foreground(socket)
                await live_on(rig, socket, f"live-{len(wrong)}")
                assert await closes_with(socket, wrong) == 1002


async def mac_still_listens(rig: Rig, request: str) -> None:
    rig.mac.wake()
    await until(lambda: rig.voice.state == "listening")
    rig.mac.say(request)
    await until(lambda: any(request in m for m in rig.requests()))


async def test_turning_live_mode_off_closes_the_phone_input_and_the_mac_keeps_listening(tmp_path: Path) -> None:
    async with live_rig(tmp_path) as rig, connect(rig.session, rig.door, rig.phones[0].token) as socket:
        await foreground(socket)
        await live_on(rig, socket)

        assert (await control(socket, "off", "set_live", on=False))["result"] == {"on": False}

        await asyncio.to_thread(rig.threads[0].join, 2)
        assert not rig.threads[0].is_alive()
        await mac_still_listens(rig, "Status of the Mac")


async def test_a_closed_socket_closes_the_phone_input_and_the_mac_keeps_listening(tmp_path: Path) -> None:
    async with live_rig(tmp_path) as rig:
        async with connect(rig.session, rig.door, rig.phones[0].token) as socket:
            await foreground(socket)
            await live_on(rig, socket)

        await asyncio.to_thread(rig.threads[0].join, 2)
        assert not rig.threads[0].is_alive()
        await mac_still_listens(rig, "Status of the Mac")


async def test_unpairing_closes_the_phone_input_and_the_mac_keeps_listening(tmp_path: Path) -> None:
    async with live_rig(tmp_path) as rig, connect(rig.session, rig.door, rig.phones[0].token) as socket:
        await foreground(socket)
        await live_on(rig, socket)

        rig.pairing.revoke("One")

        await asyncio.to_thread(rig.threads[0].join, 2)
        assert not rig.threads[0].is_alive()
        await mac_still_listens(rig, "Status of the Mac")


async def test_a_replacement_socket_starts_with_live_mode_off_and_closes_the_old_input(tmp_path: Path) -> None:
    async with live_rig(tmp_path) as rig:
        async with connect(rig.session, rig.door, rig.phones[0].token) as old:
            await foreground(old)
            await live_on(rig, old)
            async with connect(rig.session, rig.door, rig.phones[0].token) as new:
                await foreground(new)
                await asyncio.to_thread(rig.threads[0].join, 2)
                assert not rig.threads[0].is_alive()
                assert await closes_with(new, pcm(10)) == 1002
