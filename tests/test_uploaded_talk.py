from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast

import aiohttp
import numpy as np
import pytest

from jarvis_hub.clients import Client
from jarvis_hub.hub import Hub, Line
from jarvis_hub.door import Door
from jarvis_hub.event_log import EventLog, EventOutput
from jarvis_hub.pairing import PairingStore
from jarvis_hub.voice.player import Samples
from tests.fakes import FakeBackend, FakeClock, RecordingMetrics, RecordingOutput, Text, make_hub, until
from tests.protocol_exchanges import command, connect, receive


class Transcriber:
    def __init__(self, text: str = 'Good evening, how are you?') -> None:
        self.text = text
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def transcribe_buffer(self, audio: Samples) -> str:
        self.calls += 1
        assert audio.dtype == np.float32
        assert audio[0] == -1.0 and audio[1] == pytest.approx(32767 / 32768)
        self.entered.set()
        await self.release.wait()
        return self.text


async def result(socket: aiohttp.ClientWebSocketResponse, id: str) -> dict[str, Any]:
    while True:
        frame = await receive(socket)
        if frame.get('id') == id:
            return frame


@asynccontextmanager
async def rig(root: Path, transcriber: Transcriber | None = None, **options: Any):
    clock = options.pop('clock', FakeClock())
    backend = FakeBackend(clock, options.pop('talker', [[Text('Good evening, sir.')], [Text('Buonasera, sir.')]]),
                          orchestrator=options.pop('orchestrator', []))
    with PairingStore(root / 'pairing.db') as pairing, EventLog(root / 'events.db', wall=lambda: 1000) as log:
        phone = pairing.exchange(pairing.create_code('https://mac.example.ts.net'), 'Phone', 'iphone')
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), RecordingMetrics(), **options)
        asr = transcriber or Transcriber()
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log,
                    now=clock.now, transcription=asr, sleep=clock.sleep, wall=lambda: 1000)
        await hub.start()
        await door.start()
        try:
            async with aiohttp.ClientSession() as session, connect(session, door, phone.token) as socket:
                yield session, socket, door, hub, log, backend, asr, phone, clock, pairing
        finally:
            await door.close()
            await hub.close()


def client(hub: Hub, id: str) -> Client:
    found = hub.clients.get(id)
    assert found is not None
    return found


def headers(token: str, id: str = 'upload') -> dict[str, str]:
    return {'Authorization': f'Bearer {token}', 'Idempotency-Key': id,
            'Content-Type': 'application/octet-stream', 'X-Jarvis-Audio-Format': 'pcm_s16le_16000_mono'}


PCM = b'\x00\x80\xff\x7f'


async def test_uploaded_talk_is_one_spoken_turn_and_exact_retry(tmp_path: Path) -> None:
    async with rig(tmp_path) as (session, socket, door, hub, log, backend, asr, phone, _clock, _pairing):
        await socket.send_json(command('begin', 'talk_begin', talk_id='talk-1'))
        assert (await result(socket, 'begin'))['ok'] is True
        assert client(hub, phone.client.id).talking
        url = f'http://127.0.0.1:{door.port}/v1/utterances/talk-1'
        async with session.post(url, headers=headers(phone.token), data=PCM) as response:
            assert response.status == 200
            receipt = await response.json()
        async with session.post(url, headers=headers(phone.token), data=PCM) as response:
            assert await response.json() == receipt
        await until(lambda: len(backend.session('talker').received) == 2)
        heard = [event for event in log.after(0) if event.type == 'heard']
        assert len(heard) == asr.calls == 1
        assert heard[0].payload == {'request_id': receipt['result']['request_id'], 'text': asr.text, 'spoken': True}
        assert heard[0].client_id == phone.client.id
        assert '<user lang="en">' in backend.session('talker').received[-1]
        assert not client(hub, phone.client.id).talking


@pytest.mark.parametrize('ending', ['cancel', 'disconnect', 'expire', 'revoke'])
async def test_late_asr_cannot_admit_cancelled_or_expired_talk(tmp_path: Path, ending: str) -> None:
    asr = Transcriber()
    asr.release.clear()
    async with rig(tmp_path, asr) as (session, socket, door, hub, log, _backend, asr, phone, clock, pairing):
        await socket.send_json(command('begin', 'talk_begin', talk_id='talk'))
        await result(socket, 'begin')
        url = f'http://127.0.0.1:{door.port}/v1/utterances/talk'
        pending = asyncio.create_task(session.post(url, headers=headers(phone.token), data=PCM))
        await asr.entered.wait()
        if ending == 'cancel':
            await socket.send_json(command('cancel', 'talk_cancel', talk_id='talk'))
            assert (await result(socket, 'cancel'))['result']['cancelled']
        elif ending == 'disconnect':
            await socket.close()
            await until(lambda: not client(hub, phone.client.id).connected)
            assert not client(hub, phone.client.id).talking
            clock.advance(31)
        elif ending == 'expire':
            clock.advance(31)
        else:
            pairing.revoke('Phone')
        asr.release.set()
        async with await pending as response:
            assert response.status == 409
        assert not [e for e in log.after(0) if e.type == 'heard']
        assert not client(hub, phone.client.id).talking
        if ending != 'revoke':
            async with session.post(url, headers=headers(phone.token, 'new-id'), data=PCM) as response:
                assert response.status == 409
        assert asr.calls == 1


async def test_concurrent_retries_conflicts_and_shared_command_namespace(tmp_path: Path) -> None:
    asr = Transcriber()
    asr.release.clear()
    async with rig(tmp_path, asr) as (session, socket, door, _hub, _log, _backend, asr, phone, _clock, _pairing):
        await socket.send_json(command('begin', 'talk_begin', talk_id='talk'))
        await result(socket, 'begin')
        url = f'http://127.0.0.1:{door.port}/v1/utterances/talk'
        first = asyncio.create_task(session.post(url, headers=headers(phone.token), data=PCM))
        await asr.entered.wait()
        second = asyncio.create_task(session.post(url, headers=headers(phone.token), data=PCM))
        async with session.post(url, headers=headers(phone.token, 'other'), data=PCM) as response:
            assert response.status == 409
            assert (await response.json())['error']['code'] == 'talk_not_active'
        async with session.post(url, headers=headers(phone.token), data=PCM + PCM) as response:
            assert (await response.json())['error']['code'] == 'id_conflict'
        await socket.send_json(command('upload', 'say_text', text='must not execute'))
        assert (await result(socket, 'upload'))['error']['code'] == 'id_conflict'
        asr.release.set()
        one, two = await asyncio.gather(first, second)
        async with one, two:
            assert await one.json() == await two.json()
        assert asr.calls == 1


@pytest.mark.parametrize(('audio', 'status'), [(b'', 400), (b'\x00', 400), (PCM * 2_400_000, 200), (PCM * 2_400_000 + b'\x00\x00', 413)])
async def test_audio_bounds_apply_to_chunked_receipt(tmp_path: Path, audio: bytes, status: int) -> None:
    async with rig(tmp_path) as (session, socket, door, _hub, _log, _backend, asr, phone, _clock, _pairing):
        await socket.send_json(command('begin', 'talk_begin', talk_id='talk'))
        await result(socket, 'begin')
        async def chunks():
            for start in range(0, len(audio), 65000):
                yield audio[start:start + 65000]
        async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/talk', headers=headers(phone.token), data=chunks()) as response:
            assert response.status == status
        assert asr.calls == (status == 200)


@pytest.mark.parametrize(('text', 'status', 'code'), [('', 422, 'empty_transcript'), ('failure', 503, 'transcription_failed')])
async def test_transcription_errors_are_private_and_admit_nothing(tmp_path: Path, text: str, status: int, code: str) -> None:
    class Failed(Transcriber):
        async def transcribe_buffer(self, audio: Samples) -> str:
            raise RuntimeError('private raw exception and transcript')
    async with rig(tmp_path, Failed() if text else Transcriber('')) as (session, socket, door, _hub, log, _backend, _asr, phone, _clock, _pairing):
        await socket.send_json(command('begin', 'talk_begin', talk_id='talk'))
        await result(socket, 'begin')
        async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/talk', headers=headers(phone.token), data=PCM) as response:
            assert response.status == status
            payload = await response.json()
            assert payload['error']['code'] == code
            assert 'private' not in str(payload)
        assert not [e for e in log.after(0) if e.type == 'heard']


async def test_italian_and_mac_floor_use_remote_spoken_scope(tmp_path: Path) -> None:
    class MacFloor:
        def listens(self, client: str) -> bool:
            return True

        def owns(self, owner: tuple[str, int]) -> bool:
            return False
        def typed_input(self, *, client: str | None = None):
            pass
        async def quiet(self):
            await asyncio.Event().wait()
        def announce(self, client: str = "terminal"):
            return False

        def text_reply(self, owner: tuple[str, int] | None, *, client: str = "terminal") -> None:
            pass
    async with rig(tmp_path, Transcriber('Buonasera, come stai?'), floor=MacFloor()) as (session, socket, door, _hub, _log, backend, _asr, phone, _clock, _pairing):
        await socket.send_json(command('begin', 'talk_begin', talk_id='spoken-1'))
        await result(socket, 'begin')
        async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/spoken-1', headers=headers(phone.token), data=PCM) as response:
            assert response.status == 200
        await until(lambda: len(backend.session('talker').received) == 2)
        assert '<user lang="it">' in backend.session('talker').received[-1]


async def test_partial_receipt_can_retry_same_id_without_admission(tmp_path: Path) -> None:
    async with rig(tmp_path) as (session, socket, door, _hub, log, _backend, asr, phone, _clock, _pairing):
        await socket.send_json(command('begin', 'talk_begin', talk_id='talk'))
        await result(socket, 'begin')
        _reader, writer = await asyncio.open_connection('127.0.0.1', door.port)
        request = ('POST /v1/utterances/talk HTTP/1.1\r\nHost: localhost\r\n'
                   f'Authorization: Bearer {phone.token}\r\nIdempotency-Key: upload\r\n'
                   'Content-Type: application/octet-stream\r\nX-Jarvis-Audio-Format: pcm_s16le_16000_mono\r\n'
                   'Content-Length: 4\r\n\r\n').encode()
        writer.write(request + PCM[:2])
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/talk', headers=headers(phone.token), data=PCM) as response:
            assert response.status == 200
        assert asr.calls == 1
        assert len([e for e in log.after(0) if e.type == 'heard']) == 1


async def test_lost_upload_result_and_talk_tombstone_survive_restart(tmp_path: Path) -> None:
    async with rig(tmp_path) as (session, socket, door, hub, log, backend, asr, phone, _clock, pairing):
        token, client_id = phone.token, phone.client.id
        await socket.send_json(command('begin', 'talk_begin', talk_id='talk'))
        await result(socket, 'begin')
        url = f'http://127.0.0.1:{door.port}/v1/utterances/talk'
        async with session.post(url, headers=headers(token), data=PCM) as response:
            assert response.status == 200
            response.close()
        admitted_id = next(e.payload['request_id'] for e in log.after(0) if e.type == 'heard')
    with PairingStore(tmp_path / 'pairing.db') as pairing, EventLog(tmp_path / 'events.db', wall=lambda: 1000) as log:
        backend = FakeBackend(FakeClock())
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), RecordingMetrics())
        asr = Transcriber()
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log, transcription=asr)
        await hub.start()
        await door.start()
        try:
            async with aiohttp.ClientSession() as session, connect(session, door, token) as socket:
                url = f'http://127.0.0.1:{door.port}/v1/utterances/talk'
                async with session.post(url, headers=headers(token), data=PCM) as response:
                    assert response.status == 200
                    receipt = await response.json()
                    assert receipt['result'] == {'talk_id': 'talk', 'request_id': admitted_id}
                async with session.post(url, headers=headers(token, 'different'), data=PCM) as response:
                    assert response.status == 409
                await socket.send_json(command('new-begin', 'talk_begin', talk_id='talk'))
                assert (await result(socket, 'new-begin'))['error']['code'] == 'talk_not_active'
                assert asr.calls == 0
                assert len([e for e in log.after(0) if e.type == 'heard' and e.client_id == client_id]) == 1
        finally:
            await door.close()
            await hub.close()


async def test_rate_counts_new_uploads_not_exact_retries_and_resets(tmp_path: Path) -> None:
    async with rig(tmp_path) as (session, socket, door, _hub, _log, _backend, asr, phone, clock, _pairing):
        for index in range(11):
            await socket.send_json(command(f'begin-{index}', 'talk_begin', talk_id=f'talk-{index}'))
            assert (await result(socket, f'begin-{index}'))['ok']
            url = f'http://127.0.0.1:{door.port}/v1/utterances/talk-{index}'
            async with session.post(url, headers=headers(phone.token, f'upload-{index}'), data=PCM) as response:
                assert response.status == (200 if index < 10 else 429)
                if index == 10:
                    assert (await response.json())['error']['code'] == 'upload_rate_limited'
                    assert int(response.headers['Retry-After']) > 0
            if index < 10:
                async with session.post(url, headers=headers(phone.token, f'upload-{index}'), data=PCM) as response:
                    assert response.status == 200
        clock.advance(60)
        await socket.send_json(command('after-begin', 'talk_begin', talk_id='after'))
        assert (await result(socket, 'after-begin'))['ok']
        async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/after', headers=headers(phone.token, 'after'), data=PCM) as response:
            assert response.status == 200
        assert asr.calls == 11


async def test_abandoned_talk_releases_floor_without_network_traffic(tmp_path: Path) -> None:
    async with rig(tmp_path) as (session, socket, door, hub, _log, _backend, asr, phone, clock, _pairing):
        await socket.send_json(command('begin', 'talk_begin', talk_id='talk'))
        await result(socket, 'begin')
        assert client(hub, phone.client.id).talking
        clock.advance(331)
        await until(lambda: not client(hub, phone.client.id).talking)
        async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/talk', headers=headers(phone.token), data=PCM) as response:
            assert response.status == 409
        assert asr.calls == 0


async def test_older_client_asr_cannot_interrupt_newer_admitted_request(tmp_path: Path) -> None:
    from tests.fakes import AwaitInterrupt, Tool
    asr = Transcriber('Old delayed request')
    asr.release.clear()
    async with rig(tmp_path, asr, talker=[[Tool('delegate', {'request': 'New current Task'}), AwaitInterrupt()]]) as (session, socket, door, hub, log, backend, asr, phone, _clock, pairing):
        other = pairing.exchange(pairing.create_code('https://mac.example.ts.net'), 'Other', 'reference')
        async with connect(session, door, other.token) as second:
            await socket.send_json(command('begin', 'talk_begin', talk_id='same-local-counter'))
            await result(socket, 'begin')
            pending = asyncio.create_task(session.post(f'http://127.0.0.1:{door.port}/v1/utterances/same-local-counter', headers=headers(phone.token), data=PCM))
            await asr.entered.wait()
            await second.send_json(command('typed', 'say_text', text='New current Task'))
            assert (await result(second, 'typed'))['ok']
            await until(lambda: bool(backend.session('talker').tool_results))
            asr.release.set()
            async with await pending as response:
                assert response.status == 409
            assert [e.payload['text'] for e in log.after(0) if e.type == 'heard'] == ['New current Task']
            assert hub.clients.focus == other.client.id
            assert not [e for e in log.after(0) if e.type == 'reply_end']


async def test_two_clients_same_talk_counter_have_distinct_scopes_and_approval_targets(tmp_path: Path) -> None:
    from tests.fakes import Gate, Tool
    finish = asyncio.Event()
    state = tmp_path / 'state.json'
    async with rig(tmp_path, state_path=state, talker=[
        [Tool('delegate', {'request': 'First Task'}), Text('First.')],
        [Tool('delegate', {'request': 'Second Task'}), Text('Second.')],
        [Tool('present_approval', {'approval_id': 'A1'}), Text('May I post Pumpkit post 812 on X now?')],
        [Tool('delegate', {'request': 'Third independent Task'}), Text('Third.')],
    ], orchestrator=[
        [Gate(finish), Tool('report', {'task_id': 'T1', 'spoken': 'May I publish?', 'needs_user': True,
            'approval': {'action': 'subagent_action', 'grants': ['x_post'], 'post': '812', 'post_action': 'post_now'}})],
    ]) as (session, socket, door, _hub, log, _backend, _asr, phone, _clock, pairing):
        other = pairing.exchange(pairing.create_code('https://mac.example.ts.net'), 'Other', 'reference')
        receipts: list[str] = []
        async with connect(session, door, other.token) as second:
            for sock, token in [(socket, phone.token), (second, other.token)]:
                await sock.send_json(command('begin', 'talk_begin', talk_id='r1'))
                assert (await result(sock, 'begin'))['ok']
                async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/r1', headers=headers(token), data=PCM) as response:
                    assert response.status == 200
                    receipts.append((await response.json())['result']['request_id'])
                await until(lambda: len([e for e in log.after(0) if e.type == 'reply_end']) == len(receipts))
            assert receipts[0] != receipts[1]
            await second.send_json(command('foreground', 'set_presence', can_show_text=True))
            assert (await result(second, 'foreground'))['ok']
            finish.set()
            await until(lambda: any(e.type == 'task_snapshot' and any(t['state'] == 'needs_user' for t in e.payload['tasks']) for e in log.after(0)))
            heard = [e for e in log.after(0) if e.type == 'heard']
            assert [(e.client_id, e.payload['request_id']) for e in heard] == [
                (phone.client.id, receipts[0]), (other.client.id, receipts[1])]
            tasks = [e.payload['tasks'] for e in log.after(0) if e.type == 'task_snapshot'][-1]
            assert [(t['id'], t['title']) for t in tasks] == [('T1', 'First Task'), ('T2', 'Second Task')]
            await until(lambda: any(e.type == 'pending_approval' and e.payload['approvals'] for e in log.after(0)))
            approvals = [e.payload['approvals'] for e in log.after(0) if e.type == 'pending_approval'][-1]
            assert approvals[0]['task_id'] == 'T1' and '812' in approvals[0]['question']
            resolve = next(t.handler for t in _backend.session('talker').spec.tools if t.name == 'resolve_scope')
            assert 'not pending' in str(await resolve({'scope_id': receipts[0], 'revision': 1, 'attempt': 0,
                'disposition': 'revise_tasks', 'task_ids': ['T2']}))
            await second.send_json(command('third', 'say_text', text='Please inspect the docs separately'))
            assert (await result(second, 'third'))['ok']
            await until(lambda: any(e.type == 'task_snapshot' and any(t['id'] == 'T3' for t in e.payload['tasks']) for e in log.after(0)))
            _hub.publish_client_snapshots(other.client.id)
            approvals = [e.payload['approvals'] for e in log.after(0) if e.type == 'pending_approval'][-1]
            assert [(a['id'], a['task_id'], a['question']) for a in approvals] == [
                ('A1', 'T1', 'May I post Pumpkit post 812 on X now?')]



@pytest.mark.parametrize("ending", ["disconnect", "eof"])
async def test_remote_text_launch_transcribes_without_any_mac_audio_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str) -> None:
    import io
    import threading
    from dataclasses import replace
    import jarvis_hub.main as entry
    import jarvis_hub.voice.engines as engines
    from jarvis_hub.voice.transcription import TranscriptionService
    from tests.test_text_mode import settings

    done = threading.Event()
    class Input(io.StringIO):
        sent = False
        def __next__(self) -> str:
            assert done.wait(5)
            if not self.sent and ending != "eof":
                self.sent = True
                return "Typed request\n"
            raise StopIteration
    class Model:
        def transcribe(self, audio: Samples, *, after: float = 0.0) -> str:
            return 'Good evening from the phone'
    services: list[Service] = []
    doors: list[Door] = []
    class Service(TranscriptionService):
        def __init__(self) -> None:
            super().__init__(Model)
            self.closes = 0
            services.append(self)
        def close(self) -> None:
            self.closes += 1
            super().close()
    class CaptureDoor(Door):
        def __init__(self, **options: Any) -> None:
            super().__init__(**options)
            doors.append(self)
    def no_audio(*args: Any, **kwargs: Any) -> None:
        pytest.fail('Remote --text must not open a Mac audio device or load microphone engines')
    monkeypatch.setattr(entry, 'TranscriptionService', Service)
    monkeypatch.setattr(entry, 'Door', CaptureDoor)
    for name in ['SoundPlayer', '_open_listener', '_open_player']:
        monkeypatch.setattr(entry, name, no_audio)
    monkeypatch.setattr(engines, 'load_engines', no_audio)
    from tests.fakes import FakeTts
    synthesizers: list[FakeTts] = []
    def tts(engine: str) -> FakeTts:
        fake = FakeTts()
        synthesizers.append(fake)
        return fake
    monkeypatch.setattr(entry, 'make_synthesizer', tts)
    config = replace(settings(tmp_path), remote=True, remote_port=0)
    with PairingStore(config.runtime_dir / 'events.sqlite3') as pairing:
        phone = pairing.exchange(pairing.create_code('https://mac.example.ts.net'), 'Phone', 'iphone')
    backend = FakeBackend(FakeClock(), [[Text('Good evening, sir.')], [Text('Typed stays silent.')]])
    running = asyncio.create_task(entry.text_mode(settings=config, environ={}, backend=backend, clock=backend.clock,
                                stdin=Input(), stdout=io.StringIO(), stderr=io.StringIO()))
    try:
        await until(lambda: bool(doors and doors[0].port))
        async with aiohttp.ClientSession() as session, connect(session, doors[0], phone.token) as socket:
            await socket.send_json(command('presence', 'set_presence', can_show_text=True))
            assert (await result(socket, 'presence'))['ok']
            assert len(synthesizers) == 1 and synthesizers[0].calls == []
            await socket.send_json(command('begin', 'talk_begin', talk_id='talk'))
            assert (await result(socket, 'begin'))['ok']
            async with session.post(f'http://127.0.0.1:{doors[0].port}/v1/utterances/talk', headers=headers(phone.token), data=PCM) as response:
                assert response.status == 200
            clips = 0
            while True:
                frame = await receive(socket)
                if frame['type'] == 'speech_clip':
                    binary = await socket.receive()
                    assert binary.type == aiohttp.WSMsgType.BINARY
                    assert len(binary.data) == frame['payload']['byte_length'] > 0
                    clips += 1
                if frame['type'] == 'status':
                    assert frame['payload']['mac_voice_on'] is False
                if frame['type'] == 'speech_end':
                    break
            assert clips == 1
            if ending == "eof":
                done.set()
                await asyncio.wait_for(asyncio.shield(running), 2)
        assert len(services) == 1
    finally:
        done.set()
        if ending == "eof" and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        else:
            await running
    assert services[0].closes == 1
    assert doors[0].port is None

    assert len(synthesizers) == 1
    assert not any(text == 'Typed stays silent.' for text, _ in synthesizers[0].calls)


async def test_independently_admitted_uploads_keep_fifo_behind_a_gated_turn(tmp_path: Path) -> None:
    from tests.fakes import Gate
    finish = asyncio.Event()
    async with rig(tmp_path, talker=[[Gate(finish), Text('Opening.')], [Text('First reply.')], [Text('Second reply.')]]) as (session, socket, door, _hub, log, backend, asr, phone, _clock, pairing):
        other = pairing.exchange(pairing.create_code('https://mac.example.ts.net'), 'Other', 'reference')
        await socket.send_json(command('opening', 'say_text', text='Opening request'))
        await result(socket, 'opening')
        await until(lambda: len(backend.session('talker').received) == 2)
        async with connect(session, door, other.token) as second:
            for sock, token, words in [(socket, phone.token, 'First independent request'), (second, other.token, 'Second independent request')]:
                await sock.send_json(command('begin', 'talk_begin', talk_id='r1'))
                assert (await result(sock, 'begin'))['ok']
                asr.text = words
                async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/r1', headers=headers(token), data=PCM) as response:
                    assert response.status == 200
            finish.set()
            await until(lambda: len([e for e in log.after(0) if e.type == 'reply_end']) == 3)
            assert backend.session('talker').received[1:] == [
                '<user lang="en" typed="true">Opening request</user>',
                '<user lang="en">First independent request</user>',
                '<user lang="en">Second independent request</user>',
            ]
            assert [e.payload['text'] for e in log.after(0) if e.type == 'reply_end'][1:] == ['First reply.', 'Second reply.']


async def test_lost_http_response_can_retry_without_another_turn_or_private_lifetime_data(tmp_path: Path) -> None:
    import json
    import sqlite3
    asr = Transcriber('Private recorded request only in retained conversation')
    asr.release.clear()
    async with rig(tmp_path, asr) as (session, socket, door, _hub, log, _backend, asr, phone, _clock, _pairing):
        await socket.send_json(command('begin', 'talk_begin', talk_id='talk'))
        await result(socket, 'begin')
        url = f'http://127.0.0.1:{door.port}/v1/utterances/talk'
        lost = asyncio.create_task(session.post(url, headers=headers(phone.token), data=PCM))
        await asr.entered.wait()
        lost.cancel()
        with pytest.raises(asyncio.CancelledError):
            await lost
        asr.release.set()
        async with session.post(url, headers=headers(phone.token), data=PCM) as response:
            assert response.status == 200
        assert asr.calls == 1
        assert len([e for e in log.after(0) if e.type == 'heard']) == 1
        with sqlite3.connect(tmp_path / 'events.db') as database:
            commands = database.execute('SELECT type, digest, status, result FROM commands').fetchall()
            talks = database.execute('SELECT * FROM talks').fetchall()
        assert asr.text not in json.dumps([commands, talks])
        assert all(len(row[1]) == 64 and row[2] == 'complete' for row in commands)
        upload = next(row for row in commands if row[0] == 'upload')
        assert set(json.loads(upload[3])['result']) == {'talk_id', 'request_id'}
        assert not any(isinstance(value, bytes) for row in talks + commands for value in row)


async def test_old_spoken_sdk_tools_are_refused_after_another_client_talk_begins(tmp_path: Path) -> None:
    from tests.fakes import AwaitInterrupt, Tool
    async with rig(tmp_path, talker=[
        [Text('Old reply.'), AwaitInterrupt(), Tool('delegate', {'request': 'Stale forbidden Task'}), Text('Stale output.')],
        [Tool('delegate', {'request': 'Current Task'}), Text('Current reply.')],
    ]) as (session, socket, door, _hub, log, backend, _asr, phone, _clock, pairing):
        other = pairing.exchange(pairing.create_code('https://mac.example.ts.net'), 'Other', 'reference')
        async with connect(session, door, other.token) as second:
            for sock, token in [(socket, phone.token), (second, other.token)]:
                await sock.send_json(command('begin', 'talk_begin', talk_id='r1'))
                assert (await result(sock, 'begin'))['ok']
                async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/r1', headers=headers(token), data=PCM) as response:
                    assert response.status == 200
                await until(lambda: len(backend.session('talker').received) >= (2 if token == phone.token else 3))
            await until(lambda: len(backend.session('talker').tool_results) == 2)
            results = backend.session('talker').tool_results
            assert 'Refused' in str(results[0].result)
            assert results[1].input['request'] == 'Current Task'
            assert 'Stale output.' not in ''.join(e.payload.get('text', '') for e in log.after(0) if e.type in ('reply_delta', 'reply_end'))


async def test_talk_ends_quiet_mode_holds_spoken_news_and_folds_exact_count(tmp_path: Path) -> None:
    from dataclasses import replace
    from jarvis_hub.voice.speech import Speech
    from tests.fakes import FakeTts, Gate, RecordingPlayer, Set, Tool
    clock = FakeClock()
    release, reported = asyncio.Event(), asyncio.Event()
    speech = Speech(FakeTts(), RecordingPlayer(), clock, metrics=RecordingMetrics(), notice=lambda text: None)
    speech.start()
    try:
        async with rig(tmp_path, clock=clock, speech=speech, talker=[
            [Tool('quiet_mode', {'on': True}), Tool('delegate', {'request': 'Check build'}), Text('Working.')],
            [Text('One update, sir. Good evening.')],
        ], orchestrator=[[Gate(release), Tool('report', {'task_id': 'T1', 'spoken': 'Build passed', 'needs_user': False}), Set(reported)]]) as (session, socket, door, hub, log, backend, asr, phone, clock, _pairing):
            hub.attach_client(replace(client(hub, phone.client.id), can_speak=True, can_show_text=True), player=RecordingPlayer())
            await socket.send_json(command('task', 'say_text', text='Check build'))
            await result(socket, 'task')
            await until(lambda: any(e.type == 'reply_end' for e in log.after(0)))
            release.set()
            await reported.wait()
            await socket.send_json(command('begin', 'talk_begin', talk_id='talk'))
            assert (await result(socket, 'begin'))['ok']
            statuses = [e.payload for e in log.after(0) if e.type == 'status' and e.client_id == phone.client.id]
            assert statuses[-1]['quiet'] is False
            assert len(backend.session('talker').received) == 2
            async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/talk', headers=headers(phone.token), data=PCM) as response:
                assert response.status == 200
            await until(lambda: len([e for e in log.after(0) if e.type == 'reply_end']) == 2)
            message = backend.session('talker').received[-1]
            assert 'away="1"' in message and 'Build passed' in message and asr.text in message
            assert len(backend.session('talker').received) == 3
    finally:
        await speech.close()


async def test_partial_http_receipt_expires_from_first_attempt_with_socket_still_connected(tmp_path: Path) -> None:
    from types import CoroutineType
    class Clock(FakeClock):
        def __init__(self) -> None:
            super().__init__()
            self.deadlines: list[float] = []
        def sleep(self, seconds: float) -> CoroutineType[Any, Any, None]:
            self.deadlines.append(seconds)
            return super().sleep(seconds)
    clock = Clock()
    async with rig(tmp_path, clock=clock) as (session, socket, door, hub, _log, _backend, asr, phone, clock, _pairing):
        await socket.send_json(command('begin', 'talk_begin', talk_id='talk'))
        await result(socket, 'begin')
        _reader, writer = await asyncio.open_connection('127.0.0.1', door.port)
        writer.write(('POST /v1/utterances/talk HTTP/1.1\r\nHost: localhost\r\n'
                      f'Authorization: Bearer {phone.token}\r\nIdempotency-Key: upload\r\n'
                      'Content-Type: application/octet-stream\r\nX-Jarvis-Audio-Format: pcm_s16le_16000_mono\r\n'
                      'Content-Length: 4\r\n\r\n').encode() + PCM[:2])
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await until(lambda: 30 in clock.deadlines)
        clock.advance(31)
        await until(lambda: not client(hub, phone.client.id).talking)
        assert client(hub, phone.client.id).connected
        async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/talk', headers=headers(phone.token), data=PCM) as response:
            assert response.status == 409
        assert asr.calls == 0


async def test_disconnected_upload_can_finish_inside_original_retry_allowance(tmp_path: Path) -> None:
    async with rig(tmp_path) as (session, socket, door, hub, _log, _backend, asr, phone, clock, _pairing):
        await socket.send_json(command('begin', 'talk_begin', talk_id='talk'))
        await result(socket, 'begin')
        await socket.close()
        await until(lambda: not client(hub, phone.client.id).connected)
        clock.advance(20)
        async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/talk', headers=headers(phone.token), data=PCM) as response:
            assert response.status == 200
        assert asr.calls == 1
        assert not client(hub, phone.client.id).talking


async def test_upload_requires_authentication_and_supported_format(tmp_path: Path) -> None:
    async with rig(tmp_path) as (session, _socket, door, _hub, _log, _backend, asr, phone, _clock, _pairing):
        url = f'http://127.0.0.1:{door.port}/v1/utterances/talk'
        for request_headers, status, code in [
            (headers('wrong'), 401, 'unauthorized'),
            ({**headers(phone.token), 'X-Jarvis-Audio-Format': 'wav'}, 415, 'unsupported_audio'),
            ({**headers(phone.token), 'Idempotency-Key': ''}, 400, 'invalid_payload'),
        ]:
            async with session.post(url, headers=request_headers, data=PCM) as response:
                assert response.status == status
                envelope = await response.json()
                assert envelope['error']['code'] == code
                if not request_headers['Idempotency-Key']:
                    assert 'id' not in envelope
        assert asr.calls == 0


async def test_socket_replacement_releases_recording_floor_and_keeps_only_retry_allowance(tmp_path: Path) -> None:
    async with rig(tmp_path) as (session, socket, door, hub, _log, _backend, asr, phone, clock, _pairing):
        await socket.send_json(command('begin', 'talk_begin', talk_id='talk'))
        await result(socket, 'begin')
        async with connect(session, door, phone.token) as replacement:
            await replacement.send_json(command('foreground', 'set_presence', can_show_text=True))
            await result(replacement, 'foreground')
            assert not client(hub, phone.client.id).talking
            clock.advance(31)
            async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/talk', headers=headers(phone.token), data=PCM) as response:
                assert response.status == 409
            assert asr.calls == 0


async def test_reconnected_begin_replaces_abandoned_talk_before_fresh_capture(tmp_path: Path) -> None:
    async with rig(tmp_path) as (session, socket, door, hub, _log, _backend, asr, phone, _clock, _pairing):
        await socket.send_json(command('old-begin', 'talk_begin', talk_id='old'))
        assert (await result(socket, 'old-begin'))['ok'] is True
        await socket.close()
        await until(lambda: not client(hub, phone.client.id).connected)
        async with connect(session, door, phone.token) as replacement:
            await replacement.send_json(command('fresh-begin', 'talk_begin', talk_id='fresh'))
            assert (await result(replacement, 'fresh-begin'))['ok'] is True
            await replacement.send_json(command('old-cancel', 'talk_cancel', talk_id='old'))
            assert (await result(replacement, 'old-cancel'))['result'] == {'talk_id': 'old', 'cancelled': False}
            assert client(hub, phone.client.id).talking
            async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/old', headers=headers(phone.token, 'old-upload'), data=PCM) as response:
                assert response.status == 409
                assert (await response.json())['error']['code'] == 'talk_not_active'
            assert asr.calls == 0
            async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/fresh', headers=headers(phone.token), data=PCM) as response:
                assert response.status == 200
            assert asr.calls == 1


async def test_replacing_transcribing_talk_cancels_asr_without_admitting_old_line(tmp_path: Path) -> None:
    class CancellableTranscriber(Transcriber):
        def __init__(self) -> None:
            super().__init__()
            self.cancelled = asyncio.Event()
            self.release.clear()

        async def transcribe_buffer(self, audio: Samples) -> str:
            try:
                return await super().transcribe_buffer(audio)
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    asr = CancellableTranscriber()
    async with rig(tmp_path, asr) as (session, socket, door, hub, log, backend, _asr, phone, _clock, _pairing):
        await socket.send_json(command('old-begin', 'talk_begin', talk_id='old'))
        assert (await result(socket, 'old-begin'))['ok']
        pending = asyncio.create_task(session.post(f'http://127.0.0.1:{door.port}/v1/utterances/old', headers=headers(phone.token, 'old-upload'), data=PCM))
        await asr.entered.wait()
        await socket.send_json(command('fresh-begin', 'talk_begin', talk_id='fresh'))
        assert (await result(socket, 'fresh-begin'))['ok']
        await asyncio.wait_for(asr.cancelled.wait(), 1)
        async with await pending as response:
            assert response.status == 409
            assert (await response.json())['error']['code'] == 'talk_not_active'
        assert not [e for e in log.after(0) if e.type == 'heard']
        assert len(backend.session('talker').received) == 1
        assert client(hub, phone.client.id).talking
        asr.release.set()
        async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/fresh', headers=headers(phone.token), data=PCM) as response:
            assert response.status == 200
        assert [e.payload['text'] for e in log.after(0) if e.type == 'heard'] == [asr.text]


async def test_replacing_one_clients_talk_keeps_another_clients_talk_active(tmp_path: Path) -> None:
    async with rig(tmp_path) as (session, socket, door, hub, _log, _backend, _asr, phone, _clock, pairing):
        other = pairing.exchange(pairing.create_code('https://mac.example.ts.net'), 'Other', 'reference')
        async with connect(session, door, other.token) as second:
            await second.send_json(command('begin', 'talk_begin', talk_id='old'))
            assert (await result(second, 'begin'))['ok']
            await socket.send_json(command('old-begin', 'talk_begin', talk_id='old'))
            assert (await result(socket, 'old-begin'))['ok']
            await socket.send_json(command('fresh-begin', 'talk_begin', talk_id='fresh'))
            assert (await result(socket, 'fresh-begin'))['ok']
            await socket.send_json(command('cancel', 'talk_cancel', talk_id='fresh'))
            assert (await result(socket, 'cancel'))['result']['cancelled']
            assert not client(hub, phone.client.id).talking
            assert client(hub, other.client.id).talking
            await second.send_json(command('cancel', 'talk_cancel', talk_id='old'))
            assert (await result(second, 'cancel'))['result']['cancelled']


async def test_rejected_reused_talk_identity_does_not_cancel_current_talk(tmp_path: Path) -> None:
    async with rig(tmp_path) as (session, socket, door, hub, _log, _backend, _asr, phone, _clock, _pairing):
        await socket.send_json(command('begin', 'talk_begin', talk_id='talk'))
        assert (await result(socket, 'begin'))['ok']
        await socket.send_json(command('reused-begin', 'talk_begin', talk_id='talk'))
        assert (await result(socket, 'reused-begin'))['error']['code'] == 'talk_not_active'
        assert client(hub, phone.client.id).talking
        async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/talk', headers=headers(phone.token), data=PCM) as response:
            assert response.status == 200


async def test_malformed_utf8_http_and_websocket_ids_are_safe_rejections(tmp_path: Path) -> None:
    import json
    async with rig(tmp_path) as (_session, socket, door, _hub, _log, _backend, asr, phone, _clock, _pairing):
        reader, writer = await asyncio.open_connection('127.0.0.1', door.port)
        writer.write(('POST /v1/utterances/talk HTTP/1.1\r\nHost: localhost\r\n'
                      f'Authorization: Bearer {phone.token}\r\n').encode() + b'Idempotency-Key: \xff\r\n' +
                     b'Content-Type: application/octet-stream\r\nX-Jarvis-Audio-Format: pcm_s16le_16000_mono\r\n'
                     b'Content-Length: 4\r\nConnection: close\r\n\r\n' + PCM)
        await writer.drain()
        wire = await asyncio.wait_for(reader.read(), 2)
        writer.close()
        await writer.wait_closed()
        assert wire.startswith(b'HTTP/1.1 400 ')
        envelope = json.loads(wire.partition(b'\r\n\r\n')[2])
        assert envelope['error']['code'] == 'invalid_payload'
        assert 'id' not in envelope
        await socket.send_json(command('\udcff', 'talk_begin', talk_id='talk'))
        while True:
            rejected = await receive(socket)
            if rejected['type'] == 'command_result':
                break
        assert rejected['ok'] is False and rejected['error']['code'] == 'invalid_payload'
        assert 'id' not in rejected
        await socket.send_json(command('valid-after-malformed', 'say_text', text='Still connected'))
        assert (await result(socket, 'valid-after-malformed'))['ok'] is True
        assert asr.calls == 0


async def test_retired_mac_line_cannot_invalidate_a_phone_talk(tmp_path: Path) -> None:
    from jarvis_hub.voice.state import Timing
    from jarvis_hub.voice.voice_mode import VoiceMode
    from tests.fakes import FakeTts, RecordingPlayer, ScriptedAudio, Tool

    clock, metrics, output = FakeClock(), RecordingMetrics(), RecordingOutput()
    audio, player = ScriptedAudio(clock), RecordingPlayer()
    voice = VoiceMode(audio=audio, tts=FakeTts(), player=player, clock=clock, metrics=metrics,
                      notice=output.notice, timing=Timing(follow_up_s=6, announce_follow_up_s=4, listen_s=6, chime_s=0.5, tail_s=0))
    voice.start()
    try:
        async with rig(tmp_path, clock=clock, speech=voice.speech, floor=voice, speak_typed=False,
                       talker=[[Tool('delegate', {'request': 'Phone Task'}), Text('Phone accepted.')]]) as (session, socket, door, hub, log, backend, asr, phone, _clock, _pairing):
            lines = cast(AsyncGenerator[Line, None], voice.lines(hub.interrupt, hub.woke, hub.admit_spoken_revision))
            queued = asyncio.ensure_future(anext(lines))
            try:
                await until(lambda: player.voices == ['ready'])
                audio.wake()
                audio.say('Retired Mac request')
                stale = await asyncio.wait_for(queued, 2)
                assert stale.request_id is not None
                assert voice.owns((stale.request_id, stale.revision))
                await socket.send_json(command('begin', 'talk_begin', talk_id='phone-talk'))
                assert (await result(socket, 'begin'))['ok']
                assert not voice.owns((stale.request_id, stale.revision))
                before = list(player.events)
                assert await hub.admit_line(stale) is None
                assert player.events == before
                assert not [e for e in log.after(0) if e.type == 'heard']
                assert backend.session('talker').tool_results == []
                async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/phone-talk', headers=headers(phone.token), data=PCM) as response:
                    assert response.status == 200
                await until(lambda: any(e.type == 'reply_end' for e in log.after(0)))
                assert [e.payload['text'] for e in log.after(0) if e.type == 'heard'] == [asr.text]
                tasks = [t for e in log.after(0) if e.type == 'task_snapshot' for t in e.payload['tasks']]
                assert tasks and {task['title'] for task in tasks} == {'Phone Task'}
            finally:
                queued.cancel()
                await asyncio.gather(queued, return_exceptions=True)
                await lines.aclose()
    finally:
        await voice.close()


async def test_five_minute_talk_retains_full_long_scope_and_approval_after_hub_restart(tmp_path: Path) -> None:
    from tests.fakes import Tool

    transcript = 'Please inspect the build and preserve every condition. ' * 120 + 'Including this final condition.'
    assert len(transcript) > 6001
    path = tmp_path / 'state.json'
    async with rig(tmp_path, Transcriber(transcript), state_path=path, talker=[
        [Tool('delegate', {'request': 'Inspect long request'}), Text('Working.')],
        [Tool('present_approval', {'approval_id': 'A1'}), Text('Shall I publish the prepared post?')],
    ], orchestrator=[[Tool('report', {'task_id': 'T1', 'spoken': 'May I publish?', 'needs_user': True,
            'approval': {'action': 'subagent_action', 'grants': ['x_post'], 'post': '812', 'post_action': 'post_now'}})]]) as (session, socket, door, _hub, log, backend, _asr, phone, _clock, _pairing):
        token = phone.token
        await socket.send_json(command('foreground', 'set_presence', can_show_text=True))
        await result(socket, 'foreground')
        await socket.send_json(command('begin', 'talk_begin', talk_id='long-talk'))
        assert (await result(socket, 'begin'))['ok']
        async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/long-talk', headers=headers(token), data=PCM * 2_400_000) as response:
            assert response.status == 200
            request_id = (await response.json())['result']['request_id']
        await until(lambda: any(e.type == 'pending_approval' and e.payload['approvals'] for e in log.after(0)))
        assert [e.payload['text'] for e in log.after(0) if e.type == 'heard'] == [transcript]
        assert backend.session('talker').received[1] == f'<user lang="en">{transcript}</user>'
        tasks = [e.payload['tasks'] for e in log.after(0) if e.type == 'task_snapshot'][-1]
        assert tasks[0]['id'] == 'T1' and tasks[0]['title'] == 'Inspect long request'
        approval = [e.payload['approvals'] for e in log.after(0) if e.type == 'pending_approval'][-1][0]
        assert approval['task_id'] == 'T1' and approval['question'] == 'Shall I publish the prepared post?'
        resolve = next(t.handler for t in backend.session('talker').spec.tools if t.name == 'resolve_scope')
        assert 'not pending' in str(await resolve({'scope_id': request_id, 'revision': 1, 'attempt': 0,
            'disposition': 'revise_tasks', 'task_ids': ['T1']}))

    with PairingStore(tmp_path / 'pairing.db') as pairing, EventLog(tmp_path / 'events.db', wall=lambda: 1000) as log:
        backend = FakeBackend(FakeClock(), [
            [Tool('approve', {'approval_id': 'A1'}), Text('Please confirm the action first.')],
            [Tool('present_approval', {'approval_id': 'A1'}), Text('Shall I publish post 812 now?')],
            [Tool('approve', {'approval_id': 'A1'}), Text('Approved.')],
        ])
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), RecordingMetrics(), state_path=path)
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        await hub.start()
        await door.start()
        try:
            async with aiohttp.ClientSession() as session, connect(session, door, token) as socket:
                fresh: list[dict[str, Any]] = []
                while not fresh or fresh[-1]['type'] != 'replay_complete':
                    fresh.append(await receive(socket))
                snapshots: list[dict[str, Any]] = []
                while not snapshots or snapshots[-1]['type'] != 'pending_approval':
                    snapshots.append(await receive(socket))
                task_snapshot = next(e['payload']['tasks'] for e in snapshots if e['type'] == 'task_snapshot')
                assert task_snapshot[0]['id'] == 'T1'
                assert snapshots[-1]['payload']['approvals'][0]['task_id'] == 'T1'
                await socket.send_json(command('status', 'say_text', text='What is the status of that request?'))
                assert (await result(socket, 'status'))['ok']
                await until(lambda: bool(backend.session('talker').tool_results))
                hub.publish_client_snapshots(phone.client.id)
                pending = [e.payload['approvals'] for e in log.after(0) if e.type == 'pending_approval'][-1]
                assert pending[0]['id'] == 'A1' and pending[0]['task_id'] == 'T1'
                assert backend.all_sessions('subagent') == []
                assert str(backend.session('talker').tool_results[0].result).startswith('Not approved:')
                await until(lambda: any(e.type == 'reply_end' and e.payload['text'] == 'Please confirm the action first.' for e in log.after(0)))
                await socket.send_json(command('present', 'say_text', text='Remind me what needs approval'))
                assert (await result(socket, 'present'))['ok']
                await until(lambda: any(e.type == 'reply_end' and e.payload['text'] == 'Shall I publish post 812 now?' for e in log.after(0)))
                await socket.send_json(command('yes', 'say_text', text='Yes, post 812 now'))
                assert (await result(socket, 'yes'))['ok']
                await until(lambda: len(backend.session('talker').tool_results) == 3)
                assert str(backend.session('talker').tool_results[-1].result).startswith("Approved A1 with the User's words")

        finally:
            await door.close()
            await hub.close()


async def test_interactive_terminal_echoes_remote_typing_once_and_preserves_local_echo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import threading
    from dataclasses import replace
    import jarvis_hub.main as entry
    from tests.fakes import FakeTts
    from tests.test_text_mode import settings

    released = threading.Event()
    stdout, stderr = io.StringIO(), io.StringIO()
    class TTY(io.StringIO):
        sent = False
        def isatty(self) -> bool:
            return True
        def __next__(self) -> str:
            assert released.wait(5)
            if self.sent:
                raise StopIteration
            self.sent = True
            stdout.write('Local typed line\n')
            return 'Local typed line\n'
    doors: list[Door] = []
    class CaptureDoor(Door):
        def __init__(self, **options: Any) -> None:
            super().__init__(**options)
            doors.append(self)
    monkeypatch.setattr(entry, 'Door', CaptureDoor)
    def tts(engine: str) -> FakeTts:
        return FakeTts()
    monkeypatch.setattr(entry, 'make_synthesizer', tts)
    config = replace(settings(tmp_path), remote=True, remote_port=0)
    with PairingStore(config.runtime_dir / 'events.sqlite3') as pairing:
        phone = pairing.exchange(pairing.create_code('https://mac.example.ts.net'), 'Phone', 'iphone')
    backend = FakeBackend(FakeClock(), [[Text('Remote reply.')], [Text('Local reply.')]])
    running = asyncio.create_task(entry.text_mode(settings=config, environ={}, backend=backend, clock=backend.clock,
        stdin=TTY(), stdout=stdout, stderr=stderr))
    try:
        await until(lambda: bool(doors and doors[0].port) and stdout.getvalue().endswith('› '))
        async with aiohttp.ClientSession() as session, connect(session, doors[0], phone.token) as socket:
            await socket.send_json(command('remote', 'say_text', text='Remote typed line'))
            receipt = await result(socket, 'remote')
            assert receipt['ok']
            await until(lambda: 'Remote reply.' in stdout.getvalue())
            assert stdout.getvalue().count('Remote typed line') == 1
    finally:
        released.set()
        await running
    assert stdout.getvalue().count('Local typed line') == 1
    assert 'Local reply.' in stdout.getvalue()
    assert '[hub] Model ready.' in stderr.getvalue()
    with EventLog(config.runtime_dir / 'events.sqlite3') as log:
        heard = [e for e in log.after(0) if e.type == 'heard']
        assert [e.payload['text'] for e in heard] == ['Remote typed line', 'Local typed line']
        assert [e.client_id for e in heard] == [phone.client.id, 'terminal']
        assert all(not e.payload['spoken'] for e in heard)
        assert heard[0].payload['request_id'] == receipt['result']['request_id']


@pytest.mark.parametrize('user_asks', [False, True], ids=['delegated-only-refused', 'user-tail-authority'])
async def test_long_uploaded_user_words_authorize_max_effort_before_and_after_restart(tmp_path: Path, user_asks: bool) -> None:
    import json
    from tests.fakes import Tool
    from tests.test_subagents import spawn

    transcript = 'Please inspect the build and retain every condition. ' * 125
    assert len(transcript) > 6000
    transcript += 'Use max effort.' if user_asks else 'Include the final condition.'
    delegated = 'Inspect build' if user_asks else 'Inspect build with max effort'
    path = tmp_path / 'state.json'
    attempt = spawn(brief='Inspect build', model='opus', effort='max', writes=False, task_id='T1')
    async with rig(tmp_path, Transcriber(transcript), state_path=path, max_effort='max',
        talker=[[Tool('delegate', {'request': delegated}), Text('Working.')]],
        orchestrator=[[Tool('schedule_check', {'after_seconds': 600, 'note': 'Recheck build', 'max_repeats': 1}), attempt,
            Tool('report', {'task_id': 'T1', 'spoken': 'Waiting for the User.', 'needs_user': True})]],
    ) as (session, socket, door, _hub, log, backend, _asr, phone, _clock, _pairing):
        await socket.send_json(command('begin', 'talk_begin', talk_id='long-authority'))
        assert (await result(socket, 'begin'))['ok']
        async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/long-authority', headers=headers(phone.token), data=PCM) as response:
            assert response.status == 200
        await until(lambda: bool(backend.all_sessions('orchestrator')) and any(r.name == 'spawn_subagent' for r in backend.session('orchestrator').tool_results))
        first = next(r.result or '' for r in backend.session('orchestrator').tool_results if r.name == 'spawn_subagent')
        if user_asks:
            assert json.loads(first)['subagent_id'] == 'S1'
            await until(lambda: bool(backend.all_sessions('subagent')))
        else:
            assert 'only when the User asks' in first
            assert backend.all_sessions('subagent') == []
        await until(lambda: len(backend.session('orchestrator').tool_results) >= 3)
        assert [e.payload['text'] for e in log.after(0) if e.type == 'heard'] == [transcript]
        assert backend.session('talker').received[1] == f'<user lang="en">{transcript}</user>'

    backend = FakeBackend(FakeClock(), orchestrator=[[attempt]])
    hub = make_hub(backend, RecordingOutput(), RecordingMetrics(), state_path=path, max_effort='max')
    await hub.start()
    try:
        await until(lambda: bool(backend.all_sessions('orchestrator')) and bool(backend.session('orchestrator').tool_results))
        restored = backend.session('orchestrator').tool_results[0].result or ''
        if user_asks:
            assert 'subagent_id' in json.loads(restored)
            await until(lambda: bool(backend.all_sessions('subagent')))
        else:
            assert 'only when the User asks' in restored
            assert backend.all_sessions('subagent') == []
    finally:
        await hub.close()


async def test_queued_upload_needs_no_talk_begin_or_socket_and_retries_exactly(tmp_path: Path) -> None:
    async with rig(tmp_path) as (session, socket, door, hub, log, backend, asr, phone, _clock, _pairing):
        await socket.close()
        await until(lambda: not client(hub, phone.client.id).connected)
        url = f'http://127.0.0.1:{door.port}/v1/utterances/queued'
        async with session.post(url, headers=headers(phone.token), data=PCM) as response:
            assert response.status == 200
            receipt = await response.json()
        async with session.post(url, headers=headers(phone.token), data=PCM) as response:
            assert await response.json() == receipt
        await until(lambda: len(backend.session('talker').received) == 2)
        heard = [event for event in log.after(0) if event.type == 'heard']
        assert len(heard) == asr.calls == 1
        assert heard[0].payload['request_id'] == receipt['result']['request_id']
        assert '<user lang="en">' in backend.session('talker').received[-1]
        assert not client(hub, phone.client.id).talking
        async with session.post(url, headers=headers(phone.token, 'another-id'), data=PCM) as response:
            assert response.status == 409
            assert (await response.json())['error']['code'] == 'talk_not_active'
        assert asr.calls == 1


async def test_queued_upload_does_not_end_the_live_talk(tmp_path: Path) -> None:
    async with rig(tmp_path) as (session, socket, door, hub, _log, _backend, asr, phone, _clock, _pairing):
        await socket.send_json(command('begin', 'talk_begin', talk_id='live'))
        await result(socket, 'begin')
        async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/queued', headers=headers(phone.token, 'old'), data=PCM) as response:
            assert response.status == 200
        assert client(hub, phone.client.id).talking
        async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/live', headers=headers(phone.token), data=PCM) as response:
            assert response.status == 200
        assert asr.calls == 2


async def test_queued_upload_from_a_device_revoked_during_asr_admits_nothing(tmp_path: Path) -> None:
    asr = Transcriber()
    asr.release.clear()
    async with rig(tmp_path, asr) as (session, _socket, door, _hub, log, _backend, asr, phone, _clock, pairing):
        pending = asyncio.create_task(session.post(f'http://127.0.0.1:{door.port}/v1/utterances/queued',
                                                   headers=headers(phone.token), data=PCM))
        await asr.entered.wait()
        pairing.revoke('Phone')
        asr.release.set()
        async with await pending as response:
            assert response.status == 409
        assert not [e for e in log.after(0) if e.type == 'heard']


@pytest.mark.parametrize(('recorded_at', 'marker'), [
    ('1970-01-01T00:06:40Z', ' delayed="10 minutes"'),
    ('1970-01-01T00:16:00Z', ''),
    ('not a time', ''),
])
async def test_late_voice_and_typed_lines_tell_the_talker_how_late(tmp_path: Path, recorded_at: str, marker: str) -> None:
    async with rig(tmp_path) as (session, socket, door, _hub, _log, backend, _asr, phone, _clock, _pairing):
        async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/queued',
                                headers={**headers(phone.token), 'X-Jarvis-Recorded-At': recorded_at}, data=PCM) as response:
            assert response.status == 200
        await until(lambda: len(backend.session('talker').received) == 2)
        assert f'<user lang="en"{marker}>' in backend.session('talker').received[-1]
        await socket.send_json(command('foreground', 'set_presence', can_show_text=True))
        await result(socket, 'foreground')
        await socket.send_json(command('typed', 'say_text', text='Ciao', recorded_at=recorded_at))
        assert (await result(socket, 'typed'))['ok'] is True
        await until(lambda: len(backend.session('talker').received) == 3)
        assert f'typed="true"{marker}>' in backend.session('talker').received[-1]


def line_headers(token: str, id: str = 'line') -> dict[str, str]:
    return {'Authorization': f'Bearer {token}', 'Idempotency-Key': id, 'Content-Type': 'application/json'}


async def test_typed_line_over_http_needs_no_socket_and_retries_exactly(tmp_path: Path) -> None:
    async with rig(tmp_path) as (session, socket, door, hub, log, backend, _asr, phone, _clock, _pairing):
        await socket.close()
        await until(lambda: not client(hub, phone.client.id).connected)
        url = f'http://127.0.0.1:{door.port}/v1/lines'
        body = {'version': 1, 'text': 'Ciao', 'recorded_at': '1970-01-01T00:06:40Z'}
        async with session.post(url, headers=line_headers(phone.token), json=body) as response:
            assert response.status == 200
            receipt = await response.json()
        assert receipt['ok'] is True and receipt['id'] == 'line'
        async with session.post(url, headers=line_headers(phone.token), json=body) as response:
            assert await response.json() == receipt
        await until(lambda: len(backend.session('talker').received) == 2)
        assert 'typed="true" delayed="10 minutes">Ciao' in backend.session('talker').received[-1]
        heard = [event for event in log.after(0) if event.type == 'heard']
        assert len(heard) == 1 and heard[0].payload['request_id'] == receipt['result']['request_id']
        async with session.post(url, headers=line_headers(phone.token), json={**body, 'text': 'Other'}) as response:
            assert response.status == 409
            assert (await response.json())['error']['code'] == 'id_conflict'


async def test_typed_line_over_http_rejects_bad_requests(tmp_path: Path) -> None:
    async with rig(tmp_path) as (session, _socket, door, _hub, log, _backend, _asr, phone, _clock, _pairing):
        url = f'http://127.0.0.1:{door.port}/v1/lines'
        for request_headers, body, status, code in [
            (line_headers('wrong'), {'version': 1, 'text': 'Hi'}, 401, 'unauthorized'),
            (line_headers(phone.token, ''), {'version': 1, 'text': 'Hi'}, 400, 'invalid_payload'),
            (line_headers(phone.token, 'blank'), {'version': 1, 'text': '  '}, 400, 'invalid_payload'),
            (line_headers(phone.token, 'long'), {'version': 1, 'text': 'x' * 16385}, 400, 'invalid_payload'),
            (line_headers(phone.token, 'old'), {'version': 2, 'text': 'Hi'}, 426, 'unsupported_version'),
        ]:
            async with session.post(url, headers=request_headers, json=body) as response:
                assert response.status == status
                assert (await response.json())['error']['code'] == code
        assert not [e for e in log.after(0) if e.type == 'heard']
