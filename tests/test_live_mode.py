"""Live mode at the loopback Door: a paired iPhone streams its microphone into the real voice floor.

The phone's Capture runs with marker-sample engines on its own thread; the Mac's input is scripted.
"""

import asyncio
import json
import threading
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import numpy as np
import pytest

from jarvis_hub.clients import TERMINAL
from jarvis_hub.door import Door
from jarvis_hub.hub import Hub, Line
from jarvis_hub.pairing import PairingResult, PairingStore
from jarvis_hub.uploads import BufferTranscriber
from jarvis_hub.voice.engines import NoEchoCancellation
from jarvis_hub.voice.input import Engines, Listening
from jarvis_hub.voice.player import Samples
from jarvis_hub.voice.live import LiveInputs
from jarvis_hub.voice.state import SpokenWake, Timing
from jarvis_hub.voice.voice_mode import VoiceMode
from tests.fakes import (
    FakeBackend, FakeClock, FakeTts, RecordingMetrics, Gate, RecordingPlayer, ScriptedAudio, Set, Step, Text, Tool, until,
)
from tests.protocol_exchanges import command, connect
from tests.test_input_pipeline import SPEECH, WAKE, Everyone, LoudVad, MarkerWake, ScriptedStt, ScriptedTurn
from tests.test_remote_speech import control, foreground, remote_rig

FRAME = 160


def pcm(frames: int, value: int = 0) -> bytes:
    return np.full(frames * FRAME, value, "<i2").tobytes()


@dataclass
class Rig:
    session: aiohttp.ClientSession
    door: Door
    hub: Hub
    voice: VoiceMode
    mac: ScriptedAudio
    speaker: RecordingPlayer
    backend: FakeBackend
    phones: list[PairingResult]
    pairing: PairingStore
    metrics: RecordingMetrics
    notices: list[str]
    clock: FakeClock
    # The threads the phone Captures loaded their engines on, one per Live mode turned on.
    threads: list[threading.Thread] = field(default_factory=lambda: [])

    def requests(self) -> list[str]:
        return [m for s in self.backend.all_sessions("talker") for m in s.received if "<user " in m]

    def rows(self, kind: str) -> list[dict[str, Any]]:
        return [r for r in self.metrics.records if r["kind"] == kind and r.get("client") == self.phones[0].client.id]

    def ready(self) -> int:
        return len(self.rows("ears_loaded"))


@asynccontextmanager
async def live_rig(tmp_path: Path, replies: list[list[Step]] | None = None,
                   orchestrator: list[list[Step]] | None = None, words: dict[int, str] | None = None,
                   transcription: BufferTranscriber | None = None,
                   listening: Listening = Listening()) -> AsyncGenerator[Rig]:
    """`words` has the phone's STT say each word whose marker samples the audio holds, in place of "Open a PR"."""
    clock, metrics, notices = FakeClock(), RecordingMetrics(), list[str]()
    mac, speaker = ScriptedAudio(clock), RecordingPlayer()
    voice = VoiceMode(audio=mac, tts=FakeTts(), player=speaker, clock=clock, metrics=metrics,
                      notice=lambda text: None, timing=Timing(6, 4, 6, .5, tail_s=0),
                      wake_refractory_s=listening.wake_refractory_s)
    threads: list[threading.Thread] = []

    def load() -> Engines:
        threads.append(threading.current_thread())
        return Engines(echo=NoEchoCancellation(), wake=MarkerWake(), vad=LoudVad(), turn=ScriptedTurn(),
                       stt=ScriptedStt(words=words or {}), gate=Everyone())

    def live(floor: VoiceMode) -> LiveInputs:
        return LiveInputs(floor, load, listening, metrics.record, notices.append, clock=clock.now)

    backend = FakeBackend(clock, replies or [[Text("Opened.")], [Text("Second.")]], orchestrator=orchestrator or [])
    async with remote_rig(tmp_path, backend=backend, voice=voice, live=live, transcription=transcription) as (session, door, hub, _log, _backend, phones, pairing):
        running = asyncio.create_task(hub.run(voice.lines(hub.interrupt, hub.woke, hub.admit_spoken_revision)))
        try:
            await until(lambda: speaker.voices == ["ready"])
            yield Rig(session, door, hub, voice, mac, speaker, backend, phones, pairing, metrics, notices, clock, threads)
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


async def live_on(rig: Rig, socket: aiohttp.ClientWebSocketResponse, id: str = "live-on") -> None:
    loaded = rig.ready()
    assert (await control(socket, id, "set_live", on=True))["result"] == {"on": True}
    await until(lambda: rig.ready() > loaded)


async def stream(rig: Rig, socket: aiohttp.ClientWebSocketResponse, frames: int, value: int = 0) -> None:
    """Sends `frames` in real time: each message arrives as the clock reaches its end."""
    for _ in range(frames // 10):
        rig.clock.advance(0.1)
        await socket.send_bytes(pcm(10, value))
        # The Door hears a socket's messages in order, so the answer means that audio has arrived.
        await control(socket, uuid.uuid4().hex, "set_live", on=True)


async def test_a_wake_word_said_to_the_phone_opens_listening_and_its_request_reaches_the_talker(tmp_path: Path) -> None:
    async with live_rig(tmp_path) as rig, connect(rig.session, rig.door, rig.phones[0].token) as socket:
        await foreground(socket)
        await live_on(rig, socket)

        await socket.send_bytes(pcm(8, WAKE))
        await until(lambda: rig.voice.state == "listening")
        await stream(rig, socket, 80, SPEECH)
        await stream(rig, socket, 100)

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

        assert await closes_with(socket, pcm(10)) == 1002
        await mac_still_listens(rig, "Status of the Mac")


async def test_the_phone_input_shows_none_of_the_mac_microphone_notices(tmp_path: Path) -> None:
    async with live_rig(tmp_path) as rig, connect(rig.session, rig.door, rig.phones[0].token) as socket:
        await foreground(socket)
        await live_on(rig, socket)

        # Digital silence long enough for the Mac's microphone to suggest macOS privacy settings.
        await stream(rig, socket, 400)
        await until(lambda: sum(r["frames"] for r in rig.rows("capture")) >= 400)

        assert rig.notices == []


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


class Phone:
    """A Live mode iPhone at the far end of a foreground socket: it reads every frame as it arrives and reports
    clips played when the test says so."""

    def __init__(self, rig: Rig, socket: aiohttp.ClientWebSocketResponse, client: str) -> None:
        self.rig, self.socket, self.client = rig, socket, client
        self.frames: list[dict[str, Any]] = []
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for message in self.socket:
            if message.type == aiohttp.WSMsgType.TEXT:
                self.frames.append(json.loads(message.data))

    async def command(self, kind: str, **payload: Any) -> dict[str, Any]:
        id = uuid.uuid4().hex
        await self.socket.send_json(command(id, kind, **payload))
        await until(lambda: any(frame.get("id") == id for frame in self.frames), 2)
        return next(frame for frame in self.frames if frame.get("id") == id)

    async def live_on(self) -> None:
        def ready() -> int:
            return len([r for r in self.rig.metrics.records if r["kind"] == "ears_loaded" and r["client"] == self.client])

        loaded = ready()
        assert (await self.command("set_live", on=True))["result"] == {"on": True}
        await until(lambda: ready() > loaded)

    async def stream(self, frames: int, value: int = 0) -> None:
        """Sends `frames` in real time, each message as the clock reaches its end."""
        for _ in range(frames // 10):
            self.rig.clock.advance(0.1)
            await self.socket.send_bytes(pcm(10, value))
            # The Door hears a socket's messages in order, so the answer means that audio has arrived.
            await self.command("set_live", on=True)

    async def wake(self) -> None:
        await self.socket.send_bytes(pcm(8, WAKE))
        await until(lambda: self.rig.voice.state == "listening")

    async def ask(self, marker: int = SPEECH) -> None:
        """Says a request, or the words `marker` stands for, then the silence that ends the turn."""
        await self.stream(80, marker)
        await self.stream(100)

    def close(self) -> None:
        self._reader.cancel()

    def ended(self) -> int:
        """How many reply streams have ended."""
        return len([frame for frame in self.frames if frame["type"] == "speech_end" and "reply_id" in frame["payload"]])

    def states(self) -> list[str]:
        return [frame["payload"]["state"] for frame in self.frames if frame["type"] == "voice_state"]

    def streams(self) -> list[tuple[str, list[str]]]:
        """Each speech stream sent here: its cue, or "reply", and the text of its clips."""
        streams: dict[str, tuple[str, list[str]]] = {}
        for frame in self.frames:
            payload = frame.get("payload", {})
            if frame["type"] == "speech_begin":
                streams[payload["speech_id"]] = (payload.get("cue", "reply"), [])
            elif frame["type"] == "speech_clip":
                streams[payload["speech_id"]][1].append(payload["text"])
        return list(streams.values())

    async def played(self) -> None:
        """Reports every reply clip sent so far finished playing."""
        for frame in [f for f in self.frames if f["type"] == "speech_clip" and "reply_id" in f["payload"]]:
            payload = frame["payload"]
            await self.command("playback_report", speech_id=payload["speech_id"], clip_index=payload["index"],
                               state="finished")


@asynccontextmanager
async def live_phone(tmp_path: Path, replies: list[list[Step]] | None = None,
                     orchestrator: list[list[Step]] | None = None,
                     words: dict[int, str] | None = None,
                     transcription: BufferTranscriber | None = None,
                     listening: Listening = Listening()) -> AsyncGenerator[tuple[Rig, Phone]]:
    async with live_rig(tmp_path, replies, orchestrator, words, transcription, listening) as rig, connect(rig.session, rig.door, rig.phones[0].token) as socket:
        await foreground(socket)
        phone = Phone(rig, socket, rig.phones[0].client.id)
        try:
            await phone.live_on()
            yield rig, phone
        finally:
            phone.close()


async def test_a_phone_exchange_chimes_replies_and_closes_on_the_phone_alone(tmp_path: Path) -> None:
    async with live_phone(tmp_path) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await until(lambda: phone.ended() == 1)

        assert phone.streams() == [("chime", [""]), ("reply", ["Opened."])]
        # The reply has not been heard until the phone says so, however long it takes.
        rig.clock.advance(10)
        await asyncio.sleep(0.05)
        assert phone.states()[-1] == "speaking"
        # The phone's playback is the floor's own speech, which must not mute the Mac or reset the floor.
        assert not rig.hub.clients.remote_activity

        await phone.played()
        rig.clock.advance(0.1)
        await until(lambda: phone.states()[-1] == "follow_up")
        rig.clock.advance(6)
        await until(lambda: phone.states()[-1] == "idle")

        await until(lambda: len(phone.streams()) == 3)
        assert phone.streams()[-1] == ("closing", [""])
        assert phone.states() == ["idle", "listening", "thinking", "speaking", "follow_up", "idle"]
        assert rig.speaker.voices == ["ready"]
        assert [row["client"] for row in rig.metrics.records if row["kind"] == "speech"] == [rig.phones[0].client.id]


async def heard_out(rig: Rig, phone: Phone, replies: int = 1) -> None:
    """Waits for the phone's `replies`th reply to be whole, then has the phone play it to the end."""
    await until(lambda: len([s for s in phone.streams() if s[0] == "reply"]) >= replies and phone.ended() >= replies)
    await phone.played()
    # The phone streams on while time passes, so its audio keeps pace with the clock.
    await phone.stream(10)
    await until(lambda: phone.states()[-1] == "follow_up")


async def test_speaking_in_the_phones_follow_up_window_is_the_next_turn_without_the_wake_word(tmp_path: Path) -> None:
    async with live_phone(tmp_path) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await heard_out(rig, phone)

        await phone.ask()

        await until(lambda: len(rig.requests()) == 2)
        await heard_out(rig, phone, 2)
        assert [stream for stream in phone.streams() if stream[0] == "reply"] == [("reply", ["Opened."]), ("reply", ["Second."])]


async def test_standing_by_from_the_phone_stops_the_reply_there_and_closes_there(tmp_path: Path) -> None:
    told = asyncio.Event()
    async with live_phone(tmp_path, [[Text("Certainly, sir. "), Gate(told), Tool("stand_by")]]) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await until(lambda: phone.states()[-1:] == ["speaking"])

        told.set()

        await until(lambda: phone.streams()[-1:] == [("closing", [""])])
        reply = [frame["type"] for frame in phone.frames
                 if frame["type"].startswith("speech_") and "reply_id" in frame["payload"]]
        assert reply == ["speech_begin", "speech_clip", "speech_stop"]
        assert phone.states() == ["idle", "listening", "thinking", "speaking", "idle"]
        assert rig.speaker.voices == ["ready"]


async def test_the_request_after_standing_by_on_the_phone_is_not_marked_interrupted(tmp_path: Path) -> None:
    told = asyncio.Event()
    replies: list[list[Step]] = [[Text("Certainly, sir. "), Gate(told), Tool("stand_by")], [Text("Second.")]]
    async with live_phone(tmp_path, replies) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await until(lambda: phone.states()[-1:] == ["speaking"])
        told.set()
        await until(lambda: phone.streams()[-1:] == [("closing", [""])])
        # Past the Wake word's refractory time.
        await phone.stream(20)

        await phone.wake()
        await phone.ask()

        talker = rig.backend.session("talker")
        await until(lambda: len(talker.received) >= 2 and "Open a PR" in talker.received[-1])
        assert "<interrupted" not in talker.received[-1]


async def cut_in_on_the_reply(phone: Phone) -> None:
    """Has the phone play the reply's first sentence, then says the Wake word over the rest."""
    await until(lambda: phone.ended() == 1 and phone.states()[-1] == "speaking")
    first = next(f["payload"] for f in phone.frames if f["type"] == "speech_clip" and "reply_id" in f["payload"])
    await phone.command("playback_report", speech_id=first["speech_id"], clip_index=first["index"], state="finished")
    # Past the Wake word's refractory time.
    await phone.stream(30)
    await phone.wake()


async def test_a_stop_phrase_over_the_reply_on_the_phone_leaves_the_next_line_uninterrupted(tmp_path: Path) -> None:
    replies: list[list[Step]] = [[Text("Opened the pull request, sir. Review is next.")], [Text("All quiet.")]]
    async with live_phone(tmp_path, replies, words={SPEECH: "Open a PR", STOP: "That's all."}) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await cut_in_on_the_reply(phone)
        await phone.ask(STOP)
        await until(lambda: phone.states()[-1] == "idle")

        await rig.hub.admit_line(Line("Status", 0))

        talker = rig.backend.session("talker")
        await until(lambda: "Status" in talker.received[-1])
        assert "<interrupted" not in talker.received[-1]


async def test_a_request_over_the_reply_on_the_phone_carries_what_was_heard(tmp_path: Path) -> None:
    replies: list[list[Step]] = [[Text("Opened the pull request, sir. Review is next.")], [Text("Right away.")]]
    async with live_phone(tmp_path, replies) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await cut_in_on_the_reply(phone)
        await phone.ask()

        talker = rig.backend.session("talker")
        await until(lambda: len(talker.received) >= 2 and "Open a PR" in talker.received[-1])
        assert talker.received[-1].startswith('<interrupted spoken="Opened the pull request, sir."/>\n')


async def test_a_farewell_on_the_phone_ends_without_a_follow_up_window(tmp_path: Path) -> None:
    async with live_phone(tmp_path, [[Text("Until tomorrow, sir."), Tool("end_conversation")]]) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await until(lambda: phone.ended() == 1)
        await until(lambda: any(t.name == "end_conversation" for t in rig.backend.session("talker").tool_results))

        await phone.played()
        await phone.stream(10)

        await until(lambda: phone.states()[-1] == "idle")
        rig.clock.advance(60)
        await asyncio.sleep(0.05)
        assert phone.states() == ["idle", "listening", "thinking", "speaking", "idle"]
        assert [stream[0] for stream in phone.streams()] == ["chime", "reply"]


async def test_under_silence_the_reply_is_text_and_the_chime_and_closing_sound_still_play_on_the_phone(
    tmp_path: Path,
) -> None:
    async with live_phone(tmp_path) as (rig, phone):
        assert (await phone.command("set_silence", silence=True))["ok"]
        await phone.wake()
        await phone.ask()

        await until(lambda: any(frame["type"] == "reply_end" for frame in phone.frames))
        await until(lambda: phone.states()[-1] == "follow_up")
        rig.clock.advance(6)
        await until(lambda: phone.states()[-1] == "idle")

        await until(lambda: len(phone.streams()) == 2)
        assert phone.streams() == [("chime", [""]), ("closing", [""])]
        assert phone.states() == ["idle", "listening", "thinking", "follow_up", "idle"]


@pytest.mark.parametrize("phase", ["thinking", "speaking"])
async def test_silence_turned_on_mid_reply_opens_the_follow_up_window_when_the_text_ends(
    tmp_path: Path, phase: str,
) -> None:
    told = asyncio.Event()
    before: list[Step] = [Text("Certainly, sir. ")] if phase == "speaking" else []
    async with live_phone(tmp_path, [[*before, Gate(told), Text("Done.")]]) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await until(lambda: phone.states()[-1:] == [phase] and len(rig.requests()) == 1)

        assert (await phone.command("set_silence", silence=True))["ok"]
        told.set()

        await until(lambda: any(frame["type"] == "reply_end" for frame in phone.frames))
        await until(lambda: phone.states()[-1] == "follow_up")
        rig.clock.advance(6)
        await until(lambda: phone.states()[-1] == "idle")
        await until(lambda: phone.streams()[-1:] == [("closing", [""])])


async def test_voice_state_follows_set_live_and_another_live_phone_sees_only_idle(tmp_path: Path) -> None:
    async with live_rig(tmp_path) as rig, connect(rig.session, rig.door, rig.phones[0].token) as one, \
            connect(rig.session, rig.door, rig.phones[1].token) as two:
        await foreground(one)
        await foreground(two)
        asking, other = Phone(rig, one, rig.phones[0].client.id), Phone(rig, two, rig.phones[1].client.id)
        try:
            await asking.live_on()
            await other.live_on()
            await until(lambda: other.states() == ["idle"])
            kinds = [frame.get("type") for frame in other.frames]
            assert kinds.index("command_result") < kinds.index("voice_state")

            await asking.wake()
            await asking.ask()
            await heard_out(rig, asking)

            assert asking.states() == ["idle", "listening", "thinking", "speaking", "follow_up"]
            assert other.states() == ["idle"]
            assert other.streams() == []
        finally:
            asking.close()
            other.close()


async def test_a_wake_word_on_the_mac_while_the_phone_listens_moves_the_exchange_and_the_phone_sees_idle(
    tmp_path: Path,
) -> None:
    async with live_phone(tmp_path) as (rig, phone):
        await phone.wake()
        await until(lambda: phone.states()[-1] == "listening")
        # Past the Wake word's refractory time, when it is no longer the same phrase.
        rig.clock.advance(3)

        rig.mac.wake()

        await until(lambda: phone.states()[-1] == "idle")
        assert rig.voice.state == "listening"
        assert rig.voice.voice_state(TERMINAL) == "listening"
        assert rig.speaker.voices == ["ready", "chime"]


async def test_a_second_exchange_on_the_phone_stops_no_stream_but_the_one_playing(tmp_path: Path) -> None:
    async with live_phone(tmp_path) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await heard_out(rig, phone)
        rig.clock.advance(6)
        await until(lambda: phone.streams()[-1:] == [("closing", [""])])

        await phone.stream(10)
        await phone.wake()
        await phone.ask()
        await heard_out(rig, phone, 2)

        assert_one_stream_at_a_time(phone)
        assert [stream[0] for stream in phone.streams()] == ["chime", "reply", "closing", "chime", "reply"]


def assert_one_stream_at_a_time(phone: Phone) -> None:
    """The phone plays one stream at a time, so a clip, end or stop for any but the latest begun is malformed there."""
    current = None
    for frame in phone.frames:
        if frame["type"] == "speech_begin":
            current = frame["payload"]["speech_id"]
        elif frame["type"] in ("speech_clip", "speech_end", "speech_stop"):
            assert frame["payload"]["speech_id"] == current


async def test_stopping_the_reply_on_the_phone_idles_the_voice_there(tmp_path: Path) -> None:
    async with live_phone(tmp_path) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await until(lambda: phone.ended() == 1 and phone.states()[-1] == "speaking")
        reply = next(f["payload"]["speech_id"] for f in phone.frames if f["type"] == "speech_begin" and "reply_id" in f["payload"])

        assert (await phone.command("interrupt", speech_id=reply, last_clip_index=-1))["ok"]

        await until(lambda: phone.states()[-1] == "idle")
        rig.clock.advance(60)
        await asyncio.sleep(0.05)
        assert [stream[0] for stream in phone.streams()] == ["chime", "reply"]


async def test_news_for_the_phone_waits_out_its_follow_up_window(tmp_path: Path) -> None:
    arrival, reported = asyncio.Event(), asyncio.Event()
    replies: list[list[Step]] = [[Tool("delegate", {"request": "Fix typo", "context": ""}), Text("Working, sir.")],
                                 [Text("The fix is ready.")]]
    orchestrator: list[list[Step]] = [[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"}), Set(reported)]]
    async with live_phone(tmp_path, replies, orchestrator) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await heard_out(rig, phone)

        arrival.set()
        await reported.wait()
        await asyncio.sleep(0.05)
        assert [stream for stream in phone.streams() if stream[0] == "reply"] == [("reply", ["Working, sir."])]

        rig.clock.advance(6)
        await until(lambda: [s for s in phone.streams() if s[0] == "reply"][-1:] == [("reply", ["The fix is ready."])])
        assert [stream[0] for stream in phone.streams()] == ["chime", "reply", "closing", "reply"]


async def test_a_replay_on_a_live_phone_is_remote_activity_that_the_wake_word_cannot_cut_into(tmp_path: Path) -> None:
    async with live_phone(tmp_path) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await heard_out(rig, phone)
        rig.clock.advance(6)
        await until(lambda: phone.streams()[-1:] == [("closing", [""])])
        reply = next(f["payload"]["reply_id"] for f in phone.frames if f["type"] == "speech_begin" and "reply_id" in f["payload"])

        assert (await phone.command("replay_speech", reply_id=reply))["ok"]
        await until(lambda: len(phone.streams()) == 4 and rig.hub.clients.remote_activity)
        await phone.stream(10)
        await phone.socket.send_bytes(pcm(8, WAKE))
        await phone.stream(10)

        assert rig.voice.state == "idle"
        assert [stream[0] for stream in phone.streams()] == ["chime", "reply", "closing", "reply"]
        assert not phone.socket.closed


@pytest.mark.parametrize("leaving", ["set_live", "close"])
async def test_the_phone_leaving_live_mode_ends_its_exchange_and_the_mac_needs_the_wake_word(
    tmp_path: Path, leaving: str,
) -> None:
    async with live_phone(tmp_path, [[Text("Opened.")], [Text("On the Mac.")]]) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await heard_out(rig, phone)

        if leaving == "set_live":
            assert (await phone.command("set_live", on=False))["result"] == {"on": False}
        else:
            await phone.socket.close()
        await until(lambda: rig.voice.state == "idle")
        rig.mac.say("Status of the Mac")
        await asyncio.sleep(0.05)
        assert len(rig.requests()) == 1

        await mac_still_listens(rig, "Status of the Mac")
        await until(lambda: rig.speaker.texts[-1:] == ["On the Mac."])
        assert rig.hub.clients.focus == TERMINAL
        assert rig.speaker.voices[:2] == ["ready", "chime"]


async def test_turning_live_mode_off_mid_reply_stops_the_reply_there_and_leaves_the_mac_listening(
    tmp_path: Path,
) -> None:
    async with live_phone(tmp_path) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await until(lambda: phone.ended() == 1 and phone.states()[-1] == "speaking")
        assert not rig.hub.clients.remote_activity

        assert (await phone.command("set_live", on=False))["result"] == {"on": False}

        await until(lambda: any(frame["type"] == "speech_stop" for frame in phone.frames))
        assert rig.voice.state == "idle"
        assert not rig.hub.clients.remote_activity
        await mac_still_listens(rig, "Status of the Mac")


async def test_a_reply_to_a_phone_exchange_ended_by_leaving_live_mode_does_not_play_there(tmp_path: Path) -> None:
    released = asyncio.Event()
    async with live_phone(tmp_path, [[Gate(released), Text("Opened.")]]) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await until(lambda: phone.states()[-1] == "thinking")

        assert (await phone.command("set_live", on=False))["result"] == {"on": False}
        await until(lambda: rig.voice.state == "idle")
        released.set()

        await until(lambda: any(frame["type"] == "reply_end" for frame in phone.frames))
        await asyncio.sleep(0.05)
        assert phone.streams() == [("chime", [""])]
        assert not rig.hub.clients.remote_activity
        await mac_still_listens(rig, "Status of the Mac")


STOP = 1500


@pytest.mark.parametrize("phrase", ["That's all.", "Stand by."])
async def test_a_stop_phrase_in_the_phones_follow_up_window_closes_there_and_idles(tmp_path: Path, phrase: str) -> None:
    async with live_phone(tmp_path, words={SPEECH: "Open a PR", STOP: phrase}) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await heard_out(rig, phone)
        await phone.stream(50)

        await phone.ask(STOP)

        await until(lambda: phone.states()[-1] == "idle")
        await until(lambda: phone.streams()[-1:] == [("closing", [""])])
        assert [stream[0] for stream in phone.streams()] == ["chime", "reply", "closing"]
        assert len(rig.requests()) == 1
        assert rig.speaker.voices == ["ready"]


async def test_be_quiet_said_over_the_reply_on_the_phone_stops_it_there_and_closes_there(tmp_path: Path) -> None:
    replies: list[list[Step]] = [[Text("Opened the pull request, sir, and asked for a review.")], [Tool("stand_by")]]
    async with live_phone(tmp_path, replies, words={SPEECH: "Open a PR", STOP: "Be quiet."}) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await until(lambda: phone.ended() == 1 and phone.states()[-1] == "speaking")

        await phone.ask(STOP)

        await until(lambda: phone.streams()[-1:] == [("closing", [""])])
        await until(lambda: phone.states()[-1] == "idle")
        reply = [frame["type"] for frame in phone.frames
                 if frame["type"].startswith("speech_") and "reply_id" in frame["payload"]]
        assert reply == ["speech_begin", "speech_clip", "speech_end", "speech_stop"]
        assert phone.states()[-3:] == ["listening", "thinking", "idle"]
        assert any(t.name == "stand_by" for t in rig.backend.session("talker").tool_results)
        assert rig.speaker.voices == ["ready"]


async def test_a_phone_that_never_reports_its_reply_played_gives_the_floor_back_without_mixing_streams(
    tmp_path: Path,
) -> None:
    async with live_phone(tmp_path) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await until(lambda: phone.ended() == 1 and phone.states()[-1] == "speaking")

        rig.clock.advance(31)
        await until(lambda: phone.states()[-1] == "follow_up")
        rig.clock.advance(6)
        await until(lambda: phone.streams()[-1:] == [("closing", [""])])
        await phone.stream(10)
        await phone.wake()
        await phone.ask()
        await until(lambda: phone.ended() == 2)

        assert_one_stream_at_a_time(phone)
        assert [stream[0] for stream in phone.streams()] == ["chime", "reply", "closing", "chime", "reply"]
        assert not phone.socket.closed


async def test_a_cue_takes_playback_reports_of_a_known_state_only(tmp_path: Path) -> None:
    async with live_phone(tmp_path) as (_rig, phone):
        await phone.wake()
        await until(lambda: phone.streams() == [("chime", [""])])
        chime = next(f["payload"]["speech_id"] for f in phone.frames if f["type"] == "speech_begin")

        finished = await phone.command("playback_report", speech_id=chime, clip_index=0, state="finished")
        bogus = await phone.command("playback_report", speech_id=chime, clip_index=0, state="paused")

        assert finished["ok"]
        assert not bogus["ok"] and bogus["error"]["code"] == "invalid_payload"


@pytest.mark.parametrize("first", ["mac", "phone"])
async def test_the_wake_word_heard_on_the_mac_and_the_phone_opens_one_exchange_and_one_request(
    tmp_path: Path, first: str,
) -> None:
    async with live_phone(tmp_path) as (rig, phone):
        if first == "mac":
            rig.mac.wake()
            await until(lambda: rig.voice.state == "listening")
        else:
            await phone.wake()
        # Past the chime's own guard, still within the Wake word's refractory time.
        rig.clock.advance(1)

        if first == "mac":
            await phone.socket.send_bytes(pcm(8, WAKE))
            await phone.stream(100)
            await until(lambda: sum(r["frames"] for r in rig.rows("capture")) >= 100)
            await asyncio.sleep(0.05)
            rig.mac.say("Status of the Mac")
        else:
            rig.mac.wake()
            await asyncio.sleep(0.05)
            await phone.ask()

        await until(lambda: len(rig.requests()) == 1)
        await asyncio.sleep(0.05)
        assert len(rig.requests()) == 1
        chimes = [s for s in phone.streams() if s[0] == "chime"]
        if first == "mac":
            assert "Status of the Mac" in rig.requests()[0]
            await until(lambda: "Opened." in rig.speaker.texts)
            assert (rig.speaker.voices.count("chime"), chimes, phone.states()) == (1, [], ["idle"])
        else:
            assert "Open a PR" in rig.requests()[0]
            await until(lambda: phone.ended() == 1)
            assert (rig.speaker.voices, len(chimes)) == (["ready"], 1)
            assert phone.states()[:3] == ["idle", "listening", "thinking"]


async def test_speech_on_the_mac_is_not_captured_while_the_phone_holds_the_exchange(tmp_path: Path) -> None:
    async with live_phone(tmp_path) as (rig, phone):
        await phone.wake()

        rig.mac.say("Status of the Mac")
        await phone.ask()

        await until(lambda: len(rig.requests()) == 1)
        await asyncio.sleep(0.05)
        assert len(rig.requests()) == 1 and "Open a PR" in rig.requests()[0]
        assert rig.mac.capturing[-1] is False


async def test_speech_on_the_phone_is_not_captured_while_the_mac_holds_the_exchange(tmp_path: Path) -> None:
    async with live_phone(tmp_path) as (rig, phone):
        rig.mac.wake()
        await until(lambda: rig.voice.state == "listening")

        await phone.ask()
        rig.mac.say("Status of the Mac")

        await until(lambda: len(rig.requests()) == 1)
        await asyncio.sleep(0.05)
        assert len(rig.requests()) == 1 and "Status of the Mac" in rig.requests()[0]
        assert rig.rows("turn") == []
        assert phone.states() == ["idle"]


async def test_a_later_wake_word_on_the_mac_stops_the_reply_on_the_phone_and_takes_the_exchange(tmp_path: Path) -> None:
    async with live_phone(tmp_path, [[Text("Opened.")], [Text("On the Mac.")]]) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await until(lambda: phone.ended() == 1 and phone.states()[-1] == "speaking")
        # Past the Wake word's refractory time.
        await phone.stream(250)

        rig.mac.wake()

        await until(lambda: any(frame["type"] == "speech_stop" for frame in phone.frames))
        await until(lambda: rig.speaker.voices == ["ready", "chime"])
        assert phone.states()[-1] == "idle"
        rig.mac.say("Status of the Mac")
        await until(lambda: "On the Mac." in rig.speaker.texts)
        assert [stream[0] for stream in phone.streams()] == ["chime", "reply"]


async def test_a_later_wake_word_on_the_phone_stops_the_reply_on_the_mac_and_chimes_there(tmp_path: Path) -> None:
    async with live_phone(tmp_path, [[Text("On the Mac.")], [Text("Opened.")]]) as (rig, phone):
        rig.speaker.hold = True
        rig.mac.wake()
        # Past the Wake word's refractory time, before the Mac's reply would be taken to have stalled.
        await phone.stream(250)
        rig.mac.say("Status of the Mac")
        await until(lambda: rig.voice.state == "speaking")

        await phone.wake()

        await until(lambda: rig.speaker.events[-1:] == ["stop"])
        await until(lambda: phone.streams() == [("chime", [""])])
        assert rig.voice.voice_state(TERMINAL) == "idle"
        await phone.ask()
        await until(lambda: phone.ended() == 1)
        assert phone.streams() == [("chime", [""]), ("reply", ["Opened."])]
        assert "Opened." not in rig.speaker.texts


async def test_a_later_wake_word_on_the_phone_while_the_mac_listens_chimes_on_the_phone(tmp_path: Path) -> None:
    async with live_phone(tmp_path) as (rig, phone):
        rig.mac.wake()
        await until(lambda: rig.voice.state == "listening")
        await phone.stream(250)

        await phone.wake()

        await until(lambda: phone.streams() == [("chime", [""])])
        assert phone.states() == ["idle", "listening"]
        assert rig.speaker.voices == ["ready", "chime"]


class TalkTranscriber:
    """Hears every Talk as the same request."""

    async def transcribe_buffer(self, audio: Samples) -> str:
        return "Status by Talk"


async def talk(phone: Phone, rig: Rig) -> None:
    """Holds the Talk key on the phone, then sends what it recorded."""
    talk_id = uuid.uuid4().hex
    assert (await phone.command("talk_begin", talk_id=talk_id))["ok"]
    url = f"http://127.0.0.1:{rig.door.port}/v1/utterances/{talk_id}"
    headers = {"Authorization": f"Bearer {rig.phones[0].token}", "Idempotency-Key": uuid.uuid4().hex,
               "Content-Type": "application/octet-stream", "X-Jarvis-Audio-Format": "pcm_s16le_16000_mono"}
    async with rig.session.post(url, headers=headers, data=pcm(10, 1000)) as response:
        assert response.status == 200


async def test_a_held_talk_mutes_every_live_input_and_resets_the_floor(tmp_path: Path) -> None:
    async with live_phone(tmp_path) as (rig, phone):
        await phone.wake()
        talk_id = uuid.uuid4().hex

        assert (await phone.command("talk_begin", talk_id=talk_id))["ok"]

        await until(lambda: phone.states()[-1] == "idle")
        await phone.stream(250)
        rig.mac.wake()
        await phone.socket.send_bytes(pcm(8, WAKE))
        await phone.stream(100)
        await asyncio.sleep(0.1)
        assert rig.voice.state == "idle"
        assert phone.states() == ["idle", "listening", "idle"]
        assert rig.speaker.voices == ["ready"]

        assert (await phone.command("talk_cancel", talk_id=talk_id))["ok"]
        await mac_still_listens(rig, "Status of the Mac")


async def test_a_talks_reply_on_a_live_phone_gets_no_follow_up_window(tmp_path: Path) -> None:
    async with live_phone(tmp_path, transcription=TalkTranscriber()) as (rig, phone):
        await talk(phone, rig)
        await until(lambda: phone.ended() == 1)
        await phone.played()
        await until(lambda: not rig.hub.clients.remote_activity)

        await phone.ask()

        await asyncio.sleep(0.05)
        assert len(rig.requests()) == 1 and "Status by Talk" in rig.requests()[0]
        assert phone.states() == ["idle"]


async def test_a_talk_on_another_phone_mutes_both_live_inputs_until_it_is_discarded(tmp_path: Path) -> None:
    async with live_phone(tmp_path) as (rig, first), connect(rig.session, rig.door, rig.phones[1].token) as socket:
        await foreground(socket)
        second = Phone(rig, socket, rig.phones[1].client.id)
        try:
            await second.live_on()
            await first.wake()
            talk_id = uuid.uuid4().hex
            assert (await second.command("talk_begin", talk_id=talk_id))["ok"]
            await until(lambda: first.states()[-1] == "idle")

            # A locked Talk has the same Hub lifetime as a held one, through send or discard.
            await first.stream(1000)
            rig.mac.wake()
            for phone in (first, second):
                await phone.socket.send_bytes(pcm(8, WAKE))
                await phone.ask()
            await asyncio.sleep(.05)

            assert rig.requests() == []
            assert first.states() == ["idle", "listening", "idle"]
            assert second.states() == ["idle"]
            assert second.streams() == []
            assert rig.speaker.voices == ["ready"]
            assert (await second.command("talk_cancel", talk_id=talk_id))["ok"]
            await first.wake()
            await first.ask()
            await until(lambda: first.ended() == 1)
            assert len(rig.requests()) == 1 and "Open a PR" in rig.requests()[0]
        finally:
            second.close()


@pytest.mark.parametrize("previous_mac_wake", [False, True])
async def test_a_wake_word_on_the_phone_stops_its_talks_reply_and_listens_there(
    tmp_path: Path, previous_mac_wake: bool,
) -> None:
    replies: list[list[Step]] = [[Text("First sentence. Second sentence.")], [Text("Opened.")]]
    async with live_phone(tmp_path, replies, transcription=TalkTranscriber()) as (rig, phone):
        if previous_mac_wake:
            rig.mac.wake()
            await until(lambda: rig.voice.state == "listening")
        await talk(phone, rig)
        await until(lambda: phone.ended() == 1 and rig.hub.clients.remote_activity)
        rig.mac.wake()

        await phone.wake()

        await until(lambda: any(frame["type"] == "speech_stop" for frame in phone.frames))
        await until(lambda: phone.streams()[-1:] == [("chime", [""])])
        assert phone.states() == ["idle", "listening"]
        assert rig.speaker.voices == (["ready", "chime"] if previous_mac_wake else ["ready"])
        await phone.ask()
        await until(lambda: len(rig.requests()) == 2 and "Open a PR" in rig.requests()[-1])
        await until(lambda: phone.ended() == 2)
        assert [stream[0] for stream in phone.streams()] == ["reply", "chime", "reply"]


@pytest.mark.parametrize("refractory,delay,takes_over", [(.2, .2, True), (.75, .74, False), (.75, .75, True),
                                                       (3.0, 2.5, False), (3.0, 3.0, True)])
async def test_the_other_input_takes_over_at_the_configured_refractory_boundary(
    tmp_path: Path, refractory: float, delay: float, takes_over: bool,
) -> None:
    async with live_phone(tmp_path, listening=Listening(wake_refractory_s=refractory)) as (rig, phone):
        await phone.wake()
        woke_at = rig.rows("voice")[-1]["event_at"]
        rig.clock.advance(woke_at + delay - rig.clock.now())

        rig.mac.wake()
        rig.mac.say("Status of the Mac")
        await phone.ask()

        await until(lambda: len(rig.requests()) == 1)
        if takes_over:
            assert "Status of the Mac" in rig.requests()[0]
            await until(lambda: "Opened." in rig.speaker.texts)
            assert phone.states()[-1] == "idle"
        else:
            assert "Open a PR" in rig.requests()[0]
            await until(lambda: phone.ended() == 1)
            assert rig.speaker.voices == ["ready"]


async def test_a_wake_word_on_the_phone_replaces_the_macs_in_progress_capture(tmp_path: Path) -> None:
    async with live_phone(tmp_path) as (rig, phone):
        rig.mac.wake()
        await until(lambda: rig.voice.state == "listening")
        await phone.stream(250)
        rig.mac.speech()
        await asyncio.sleep(.05)

        await phone.wake()

        await until(lambda: phone.streams() == [("chime", [""])])
        rig.mac.say("Abandoned on the Mac")
        await phone.ask()
        await until(lambda: phone.ended() == 1)
        assert len(rig.requests()) == 1 and "Open a PR" in rig.requests()[0]
        assert rig.speaker.voices == ["ready", "chime"]


async def test_a_spoken_stop_phrase_on_the_other_input_closes_without_an_opening_chime(tmp_path: Path) -> None:
    async with live_phone(tmp_path) as (rig, phone):
        await phone.wake()
        await phone.ask()
        await until(lambda: phone.ended() == 1)
        await phone.stream(250)

        rig.mac.emit(SpokenWake("that's all", "en", rig.clock.now(), rig.clock.now() - .5))

        await until(lambda: "closing" in rig.speaker.voices)
        await until(lambda: any(frame["type"] == "speech_stop" for frame in phone.frames))
        await asyncio.sleep(.05)
        assert rig.speaker.voices == ["ready", "closing"]
        assert phone.states()[-1] == "idle"
        assert len(rig.requests()) == 1


@pytest.mark.parametrize("source", ["phone", "terminal"])
@pytest.mark.parametrize("stage", ["listening", "capturing", "speaking"])
async def test_typing_during_a_phone_exchange_idles_the_voice_and_replies_only_as_text(
    tmp_path: Path, source: str, stage: str,
) -> None:
    replies: list[list[Step]] = [[Text("Typed answer.")]]
    if stage == "speaking":
        replies.insert(0, [Text("Live answer.")])
    async with live_phone(tmp_path, replies) as (rig, phone):
        await phone.wake()
        if stage == "capturing":
            await phone.stream(100, SPEECH)
            await until(lambda: sum(row["frames"] for row in rig.rows("capture")) >= 100)
        elif stage == "speaking":
            await phone.ask()
            await until(lambda: phone.ended() == 1)
        before = phone.ended()

        if source == "phone":
            assert (await phone.command("say_text", text="Type quietly"))["ok"]
        else:
            await rig.hub.admit_line(Line("Type quietly", rig.clock.now(), typed=True))

        await until(lambda: any(frame["type"] == "reply_end" and frame["payload"].get("text") == "Typed answer."
                                for frame in phone.frames))
        await until(lambda: phone.states()[-1] == "idle")
        await phone.ask()
        await asyncio.sleep(.05)
        assert phone.ended() == before
        assert rig.speaker.voices == ["ready"]
        assert len(rig.requests()) == (2 if stage == "speaking" else 1)
        assert 'typed="true">Type quietly</user>' in rig.requests()[-1]
