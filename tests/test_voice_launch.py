"""`jarvis-hub` in voice mode, wired as the launcher wires it, with a scripted microphone and a recording player."""

import asyncio
import io
import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from jarvis_hub.agents.backend import SessionSpec
from jarvis_hub.config import RoleModel, Settings, VoiceSettings
from jarvis_hub.main import Ears, timing, voice_mode
from jarvis_hub.voice.engines import EngineChoice
from jarvis_hub.voice.input import Listening
from jarvis_hub.voice.language import Lang
from jarvis_hub.voice.player import Samples
from tests.fakes import FakeBackend, FakeClock, FakeSession, FakeTts, RecordingPlayer, ScriptedAudio, Text, until


class HeldStdin(io.StringIO):
    """A terminal nobody types into, until `release` ends the input."""

    def __init__(self) -> None:
        super().__init__()
        self.done = threading.Event()

    def __next__(self) -> str:
        self.done.wait(timeout=5)
        raise StopIteration


class ScriptedMicrophone(ScriptedAudio):
    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.started = self.closed = False
        self.ready = threading.Event()
        self.record: Callable[[dict[str, Any]], None] = lambda row: None

    def start(self) -> None:
        self.started = True
        self.ready.set()

    def close(self) -> None:
        self.closed = True

    def reference(self, samples: Samples, rate: int) -> None:
        pass

    def say(self, text: str, lang: Lang | None = None, *, limited: bool = False) -> None:
        """Records the turn as Capture does, then reports it."""
        self.record({"kind": "turn", "text": text})
        super().say(text, lang, limited=limited)


def settings(tmp_path: Path, voice: VoiceSettings = VoiceSettings()) -> Settings:
    return Settings(
        user_name="Ada",
        org="acme",
        org_dir=tmp_path,
        repo_dir=tmp_path / "jarvis",
        runtime_dir=tmp_path / "jarvis" / ".jarvis",
        talker=RoleModel("sonnet", "low", False),
        orchestrator=RoleModel("sonnet", "medium", True),
        voice=voice,
    )


@pytest.mark.parametrize("connection", ["success", "failure", "held"])
async def test_voice_mode_wakes_hears_answers_aloud_and_idles(tmp_path: Path, connection: str) -> None:
    clock = FakeClock()
    opening = asyncio.Event()
    cancelled = asyncio.Event()

    class StartupBackend(FakeBackend):
        async def open(self, spec: SessionSpec) -> FakeSession:
            if spec.role == "orchestrator":
                opening.set()
                if connection == "failure":
                    raise RuntimeError("offline")
                if connection == "held":
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()
            return await super().open(spec)

    backend = StartupBackend(clock, [[Text("Half past nine, sir.")]])
    microphone = ScriptedMicrophone(clock)
    player = RecordingPlayer()
    chosen: list[tuple[EngineChoice, Listening]] = []

    def open_listener(
        choice: EngineChoice, listening: Listening, record: Any, notice: Any, clips: Any
    ) -> ScriptedMicrophone:
        chosen.append((choice, listening))
        microphone.record = record
        return microphone

    def open_player(rate: int, on_played: Callable[[Samples, int], None]) -> RecordingPlayer:
        return player

    stdin, stdout, stderr = HeldStdin(), io.StringIO(), io.StringIO()
    config = settings(tmp_path, VoiceSettings(follow_up_s=3.0, end_silence_ms=400, max_turn_s=20.0, onset_pad_ms=300, bargein_min_speech_ms=150))
    running = asyncio.create_task(
        voice_mode(
            settings=config,
            environ={},
            backend=backend,
            clock=clock,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            ears=Ears(FakeTts(), open_player, open_listener),
            headphones=True,
        )
    )
    await until(lambda: microphone.started and player.voices == ["ready"])
    await until(opening.is_set)
    if connection == "success":
        assert backend.all_sessions("orchestrator")[0].received == []
    elif connection == "failure":
        assert "Background model unavailable: RuntimeError: offline" in stderr.getvalue()
    assert "Model loading." in stderr.getvalue()
    assert "Model ready." in stderr.getvalue()
    assert "Speech ready." in stderr.getvalue()
    microphone.wake()
    await until(lambda: microphone.capturing == [True])
    microphone.say("What time is it?")
    await until(lambda: microphone.capturing == [True, False, True])
    clock.advance(3.0)
    await until(lambda: microphone.capturing == [True, False, True, False])
    stdin.done.set()
    await asyncio.wait_for(running, timeout=5)

    assert stdout.getvalue() == "\r» What time is it?\nHalf past nine, sir.\n"
    assert player.texts == ["", "", "Half past nine, sir.", ""]
    [(choice, listening)] = chosen
    assert choice.aec == "off"
    assert (listening.end_silence_s, listening.max_turn_s, listening.onset_pad_s) == (0.4, 20.0, 0.3)
    assert listening.bargein_min_speech_s == 0.15
    assert microphone.closed and player.closed
    assert all(session.closed for session in backend.sessions)
    if connection == "held":
        assert cancelled.is_set()
    rows = [json.loads(line) for line in (config.runtime_dir / "metrics.jsonl").read_text().splitlines()]
    assert [r["to"] for r in rows if r["kind"] == "voice"] == ["listening", "thinking", "speaking", "follow_up", "idle"]
    assert {r["client"] for r in rows if r["kind"] in ("voice", "turn")} == {"terminal"}


def test_the_barge_in_windows_reach_the_voice_state_machine() -> None:
    voice = timing(VoiceSettings(bargein_decide_ms=600, bargein_guard_ms=400))

    assert (voice.decide_s, voice.guard_s) == (0.6, 0.4)


async def test_voice_launcher_remote_replay_and_interrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    import aiohttp

    import jarvis_hub.main as entry
    from jarvis_hub.door import Door
    from jarvis_hub.pairing import PairingStore
    from jarvis_hub.voice.transcription import TranscriptionService
    from tests.fakes import AwaitInterrupt
    from tests.protocol_exchanges import command, connect
    from tests.test_remote_speech import control, foreground, through

    class Transcriber:
        def transcribe(self, audio: Samples, *, after: float = 0.0) -> str:
            return 'Hello from the phone'

    class Transcription(TranscriptionService):
        def __init__(self) -> None:
            super().__init__(Transcriber)

    doors: list[Door] = []

    class ListeningDoor(Door):
        def __init__(self, **options: Any) -> None:
            super().__init__(**options)
            doors.append(self)

    monkeypatch.setattr(entry, 'TranscriptionService', Transcription)
    monkeypatch.setattr(entry, 'Door', ListeningDoor)
    config = replace(settings(tmp_path), remote=True, remote_port=0)
    with PairingStore(config.runtime_dir / 'events.sqlite3') as pairing:
        phone = pairing.exchange(pairing.create_code('https://mac.example.ts.net'), 'Phone', 'iphone')
    clock = FakeClock()
    backend = FakeBackend(clock, [[Text('Retained reply.')], [Text('Opening. '), AwaitInterrupt(), Text('Too late.')]])
    microphone, player, stdin = ScriptedMicrophone(clock), RecordingPlayer(), HeldStdin()

    def open_player(rate: int, on_played: Callable[[Samples, int], None]) -> RecordingPlayer:
        return player

    def open_listener(choice: EngineChoice, listening: Listening, record: Any, notice: Any, clips: Any) -> ScriptedMicrophone:
        return microphone

    running = asyncio.create_task(entry.voice_mode(
        settings=config, environ={}, backend=backend, clock=clock, stdin=stdin,
        stdout=io.StringIO(), stderr=io.StringIO(),
        ears=Ears(FakeTts(), open_player, open_listener), headphones=True,
    ))
    try:
        await until(lambda: microphone.started and player.voices == ['ready'])
        async with aiohttp.ClientSession() as session, connect(session, doors[0], phone.token) as socket:
            await foreground(socket)

            async def talk(identity: str) -> None:
                assert (await control(socket, identity, 'talk_begin', talk_id=identity))['ok']
                async with session.post(f'http://127.0.0.1:{doors[0].port}/v1/utterances/{identity}', data=b'\x00\x00', headers={
                    'Authorization': f'Bearer {phone.token}', 'Idempotency-Key': f'upload-{identity}',
                    'Content-Type': 'application/octet-stream', 'X-Jarvis-Audio-Format': 'pcm_s16le_16000_mono',
                }) as response:
                    assert response.status == 200

            await talk('old')
            original = await through(socket, 'speech_end')
            reply_id = next(f['payload']['reply_id'] for f in original if f['type'] == 'speech_begin')
            await socket.send_json(command('replay', 'replay_speech', reply_id=reply_id))
            replay = await through(socket, 'command_result')
            assert replay[-1]['ok']
            replay_id = replay[-1]['result']['speech_id']
            audio = await through(socket, 'speech_end')
            assert [f['payload']['text'] for f in audio if f['type'] == 'speech_clip'] == ['Retained reply.']
            assert next(f['payload'] for f in audio if f['type'] == 'speech_begin') == {
                'reply_id': reply_id, 'speech_id': replay_id, 'channels': 1, 'sample_rate': 24000, 'sample_width': 2}
            assert (await control(socket, 'stop-replay', 'interrupt', speech_id=replay_id, last_clip_index=-1))['ok']
            await talk('current')
            opening = await through(socket, 'speech_clip')
            speech_id = next(f['payload']['speech_id'] for f in opening if f['type'] == 'speech_begin')
            await socket.send_json(command('stop-current', 'interrupt', speech_id=speech_id, last_clip_index=0))
            stopped = await through(socket, 'command_result')
            assert stopped[-1]['ok']
            assert any(f['type'] == 'speech_stop' and f['payload']['speech_id'] == speech_id for f in stopped)
    finally:
        stdin.done.set()
        await asyncio.wait_for(running, 5)
    assert player.texts == ['']
    assert microphone.closed and player.closed
    assert all(session.closed for session in backend.sessions)


async def test_live_mode_hears_the_phone_through_its_own_engines_and_keeps_no_clip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    import aiohttp
    import numpy as np

    import jarvis_hub.main as entry
    import jarvis_hub.voice.engines as engines
    from jarvis_hub.door import Door
    from jarvis_hub.pairing import PairingStore
    from jarvis_hub.voice.input import Engines
    from jarvis_hub.voice.transcription import TranscriptionService
    from tests.protocol_exchanges import connect
    from tests.test_input_pipeline import SPEECH, WAKE, Everyone, LoudVad, MarkerWake, ScriptedTurn
    from tests.test_remote_speech import control, foreground

    class Transcriber:
        def transcribe(self, audio: Samples, *, after: float = 0.0) -> str:
            return 'Hello from the phone'

    class Transcription(TranscriptionService):
        def __init__(self) -> None:
            super().__init__(Transcriber)

    doors: list[Door] = []

    class ListeningDoor(Door):
        def __init__(self, **options: Any) -> None:
            super().__init__(**options)
            doors.append(self)

    loaded: list[tuple[EngineChoice, TranscriptionService]] = []

    def load_engines(choice: EngineChoice, *, transcription: TranscriptionService) -> Engines:
        loaded.append((choice, transcription))
        return Engines(echo=engines.NoEchoCancellation(), wake=MarkerWake(), vad=LoudVad(), turn=ScriptedTurn(),
                       stt=transcription, gate=Everyone())

    monkeypatch.setattr(entry, 'TranscriptionService', Transcription)
    monkeypatch.setattr(entry, 'Door', ListeningDoor)
    monkeypatch.setattr(engines, 'load_engines', load_engines)
    config = replace(settings(tmp_path, VoiceSettings(audio_clips=True)), remote=True, remote_port=0)
    with PairingStore(config.runtime_dir / 'events.sqlite3') as pairing:
        phone = pairing.exchange(pairing.create_code('https://mac.example.ts.net'), 'Phone', 'iphone')
    clock = FakeClock()
    backend = FakeBackend(clock, [[Text('Heard you.')]])
    microphone, player, stdin, stderr = ScriptedMicrophone(clock), RecordingPlayer(), HeldStdin(), io.StringIO()

    def open_player(rate: int, on_played: Callable[[Samples, int], None]) -> RecordingPlayer:
        return player

    def open_listener(choice: EngineChoice, listening: Listening, record: Any, notice: Any, clips: Any) -> ScriptedMicrophone:
        return microphone

    def pcm(frames: int, value: int = 0) -> bytes:
        return np.full(frames * 160, value, '<i2').tobytes()

    def metric_rows() -> list[dict[str, Any]]:
        path = config.runtime_dir / 'metrics.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    running = asyncio.create_task(entry.voice_mode(
        settings=config, environ={}, backend=backend, clock=clock, stdin=stdin,
        stdout=io.StringIO(), stderr=stderr, ears=Ears(FakeTts(), open_player, open_listener),
    ))
    try:
        await until(lambda: microphone.started and player.voices == ['ready'])
        async with aiohttp.ClientSession() as session, connect(session, doors[0], phone.token) as socket:
            await foreground(socket)
            assert (await control(socket, 'live', 'set_live', on=True))['result'] == {'on': True}
            await until(lambda: any(r['kind'] == 'ears_loaded' and r.get('client') == phone.client.id
                                    for r in metric_rows()))
            await socket.send_bytes(pcm(8, WAKE))
            await until(lambda: microphone.capturing[-1:] == [True])
            for value, messages in ((SPEECH, 8), (0, 10)):
                for i in range(messages):
                    clock.advance(0.1)
                    await socket.send_bytes(pcm(10, value))
                    # Answered after the Door has heard the audio sent before it.
                    await control(socket, f'sent-{value}-{i}', 'set_live', on=True)
            await until(lambda: any('Hello from the phone' in m for s in backend.all_sessions('talker') for m in s.received))
    finally:
        stdin.done.set()
        await asyncio.wait_for(running, 5)
    [(choice, transcription)] = loaded
    assert choice.aec == 'off'
    assert isinstance(transcription, Transcription)
    assert not any(path.is_file() for path in (config.runtime_dir / 'clips').rglob('*'))
    assert {r['client'] for r in metric_rows() if r['kind'] == 'turn'} == {phone.client.id}
    assert 'Listening ready' not in stderr.getvalue()
