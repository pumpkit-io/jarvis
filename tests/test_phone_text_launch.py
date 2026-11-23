"""The text launcher accepts phone Live mode without constructing a Mac microphone or speaker."""

import asyncio
import io
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import aiohttp
import pytest

import jarvis_hub.main as entry
from jarvis_hub.door import Door
from jarvis_hub.pairing import PairingStore
from jarvis_hub.voice.engines import NoEchoCancellation
from jarvis_hub.voice.input import Engines
from tests.fakes import FakeBackend, FakeClock, FakeTts, RecordingMetrics, Text, until
from tests.protocol_exchanges import command, connect
from tests.test_input_pipeline import Everyone, LoudVad, MarkerWake, ScriptedStt, ScriptedTurn, SPEECH, WAKE
from tests.test_live_mode import pcm
from tests.test_remote_speech import foreground
from tests.test_text_mode import settings
from tests.test_voice_launch import HeldStdin


async def test_text_launch_hears_a_live_phone_and_replies_there_without_opening_mac_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    doors: list[Door] = []
    class BoundDoor(Door):
        def __init__(self, **options: Any) -> None:
            super().__init__(**options)
            doors.append(self)
    def no_mac(*args: Any, **kwargs: Any) -> None:
        pytest.fail('--text opened a Mac audio device')
    def load(*args: Any, **kwargs: Any) -> Engines:
        return Engines(echo=NoEchoCancellation(), wake=MarkerWake(), vad=LoudVad(), turn=ScriptedTurn(),
                       stt=ScriptedStt(), gate=Everyone())
    metrics, clock = RecordingMetrics(), FakeClock()
    monkeypatch.setattr(entry, 'Door', BoundDoor)
    monkeypatch.setattr(entry, '_open_listener', no_mac)
    monkeypatch.setattr(entry, 'SoundPlayer', no_mac)
    monkeypatch.setattr(entry, '_open_player', no_mac)
    monkeypatch.setattr(entry, '_load_engines', load)
    def synthesizer(engine: str) -> FakeTts:
        return FakeTts()
    def recording(path: Path) -> RecordingMetrics:
        return metrics
    monkeypatch.setattr(entry, 'make_synthesizer', synthesizer)
    monkeypatch.setattr(entry, 'JsonlMetrics', recording)
    config = replace(settings(tmp_path), remote=True, remote_port=0)
    with PairingStore(config.runtime_dir / 'events.sqlite3') as pairing:
        phone = pairing.exchange(pairing.create_code('https://mac.example.ts.net'), 'Phone', 'iphone')
    backend = FakeBackend(clock, [[Text('From your phone.')]])
    stdin, stdout = HeldStdin(), io.StringIO()
    running = asyncio.create_task(entry.text_mode(settings=config, environ={}, backend=backend, clock=clock,
        stdin=stdin, stdout=stdout, stderr=io.StringIO()))
    try:
        await until(lambda: bool(doors and doors[0].port))
        async with aiohttp.ClientSession() as session, connect(session, doors[0], phone.token) as socket:
            await foreground(socket)
            received: list[dict[str, Any]] = []
            audio: list[bytes] = []
            async def read() -> None:
                async for message in socket:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        received.append(json.loads(message.data))
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        audio.append(message.data)
            async def set_live(id: str) -> None:
                await socket.send_json(command(id, 'set_live', on=True))
                await until(lambda: any(frame.get('id') == id for frame in received))
                assert next(frame for frame in received if frame.get('id') == id)['ok']
            reader = asyncio.create_task(read())
            try:
                await set_live('on')
                await until(lambda: {'ears_loaded', 'voice_loaded'} <= {r['kind'] for r in metrics.records})
                await socket.send_bytes(pcm(8, WAKE))
                await set_live('wake-barrier')
                for frames, value in [(80, SPEECH), (100, 0)]:
                    for i in range(frames // 10):
                        clock.advance(.1)
                        await socket.send_bytes(pcm(10, value))
                        await set_live(f'frame-{value}-{i}')
                await until(lambda: any(frame['type'] == 'speech_end' and frame['payload'].get('reply_id')
                                       for frame in received), 3)
                clips = [frame['payload']['text'] for frame in received
                         if frame['type'] == 'speech_clip' and frame['payload'].get('reply_id')]
                assert len(audio) >= 2 and all(audio)
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            assert clips == ['From your phone.']
            assert any('Open a PR' in m for m in backend.session('talker').received)
            assert any(r['kind'] == 'capture' and r.get('client') == phone.client.id for r in metrics.records)
    finally:
        stdin.done.set()
        await running


async def test_remote_text_speak_still_announces_news_on_the_mac_without_a_microphone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.fakes import Gate, RecordingPlayer, Tool
    arrival = asyncio.Event()
    class Input(HeldStdin):
        sent = False
        def __next__(self) -> str:
            if not self.sent:
                self.sent = True
                return 'Fix the typo\n'
            return super().__next__()
    def no_mac_input(*args: Any, **kwargs: Any) -> None:
        pytest.fail('--text --speak opened a Mac microphone')
    monkeypatch.setattr(entry, '_open_listener', no_mac_input)
    clock, player, stdin = FakeClock(), RecordingPlayer(), Input()
    backend = FakeBackend(clock, [[Tool('delegate', {'request': 'Fix typo', 'context': ''}), Text('Working.')],
                                  [Text('The fix is ready.')]],
                          orchestrator=[[Gate(arrival), Tool('report', {'task_id': 'T1', 'spoken': 'Ready'})]])
    config = replace(settings(tmp_path), remote=True, remote_port=0)
    def speaker(rate: int) -> RecordingPlayer:
        return player
    running = asyncio.create_task(entry.text_mode(settings=config, environ={}, backend=backend, clock=clock,
        stdin=stdin, stdout=io.StringIO(), stderr=io.StringIO(), voice=entry.Mouth(FakeTts(), speaker)))
    try:
        await until(lambda: 'Working.' in player.texts)
        arrival.set()
        await until(lambda: 'The fix is ready.' in player.texts)
        assert 'chime' not in player.voices and 'closing' not in player.voices
    finally:
        stdin.done.set()
        await running
