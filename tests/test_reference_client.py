import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from jarvis_hub.door import Door
from jarvis_hub.event_log import EventLog, EventOutput
from jarvis_hub.pairing import PairingStore
from tests.fakes import FakeBackend, FakeClock, RecordingMetrics, RecordingOutput, Text, make_hub


async def invoke(*args: str) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(sys.executable, 'tools/reference_client.py', *args,
                                                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    return process.returncode or 0, stdout.decode(), stderr.decode()


async def test_executable_pairs_and_streams_a_typed_reply(tmp_path: Path) -> None:
    with PairingStore(tmp_path / 'pairing.db') as pairing, EventLog(tmp_path / 'events.db') as log:
        backend = FakeBackend(FakeClock(), [[Text('Here, sir.')]])
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), RecordingMetrics())
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        await hub.start()
        await door.start()
        try:
            credentials = tmp_path / 'client' / 'credentials.json'
            common = ('--credentials', str(credentials), '--output', str(tmp_path / 'clips'))
            code = pairing.create_code('https://mac.example.ts.net')
            result, stdout, stderr = await invoke(*common, 'pair', f'http://127.0.0.1:{door.port}', '--code', code)
            assert result == 0, stderr
            secret = json.loads(credentials.read_text())['device_token']
            assert secret not in stdout + stderr and code not in stdout + stderr
            assert credentials.stat().st_mode & 0o777 == 0o600
            result, stdout, stderr = await invoke(*common, 'say', 'Hello')
            assert result == 0, stderr
            frames = [json.loads(line) for line in stdout.splitlines()]
            assert [f['payload']['text'] for f in frames if f['type'] == 'reply_delta'] == ['Here, sir.']
            assert any(f['type'] == 'reply_end' for f in frames)
            assert secret not in stdout + stderr
            result, history, stderr = await invoke(*common, 'history')
            assert result == 0, stderr
            assert any(json.loads(line)['type'] == 'reply_end' for line in history.splitlines())
        finally:
            await door.close()
            await hub.close()


async def test_wav_upload_is_pcm_and_saved_reply_is_playable(tmp_path: Path) -> None:
    import wave

    from jarvis_hub.voice.speech import Speech
    from tests.fakes import FakeTts, RecordingPlayer
    from tests.test_uploaded_talk import Transcriber

    wav_path = tmp_path / 'utterance.wav'
    with wave.open(str(wav_path), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b'\x00\x80\xff\x7f')
    clock, metrics = FakeClock(), RecordingMetrics()
    with PairingStore(tmp_path / 'pairing.db') as pairing, EventLog(tmp_path / 'events.db') as log:
        speech = Speech(FakeTts(), RecordingPlayer(), clock, metrics=metrics, notice=lambda _: None)
        backend = FakeBackend(clock, [[Text('First sentence. Second sentence.')]])
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), metrics, speech=speech)
        transcriber = Transcriber()
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log, transcription=transcriber)
        speech.start()
        await hub.start()
        await door.start()
        try:
            common = ('--credentials', str(tmp_path / 'client/credentials.json'), '--output', str(tmp_path / 'clips'))
            assert (await invoke(*common, 'pair', f'http://127.0.0.1:{door.port}', '--code', pairing.create_code('https://mac.example.ts.net')))[0] == 0
            code, stdout, stderr = await invoke(*common, 'talk', str(wav_path))
            assert code == 0, stderr
            frames = [json.loads(line) for line in stdout.splitlines()]
            clips = [f for f in frames if f['type'] == 'saved_clip']
            assert len(clips) == 2
            with wave.open(clips[0]['path']) as wav:
                assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (24000, 1, 2)
                assert wav.readframes(wav.getnframes()) == b'\xcc\x0c' * (len('First sentence.') * 1200)
            timing = next(f for f in frames if f['type'] == 'upload_metrics')
            assert timing['release_to_first_clip_seconds'] >= timing['handoff_to_first_clip_seconds']
            assert timing['upload_duration_seconds'] >= 0
            assert timing['playback_boundary'] == 'clip_received_not_physical_playback'
            assert transcriber.calls == 1
        finally:
            await door.close()
            await hub.close()
            await speech.close()


async def test_mid_reply_reconnect_caches_each_event_once_and_retries_same_command(tmp_path: Path) -> None:
    from tools.reference_client import ReferenceClient, pair
    from tests.fakes import Gate, until

    release = asyncio.Event()
    with PairingStore(tmp_path / 'pairing.db') as pairing, EventLog(tmp_path / 'events.db') as log:
        backend = FakeBackend(FakeClock(), [[Text('One '), Gate(release), Text('two.')]])
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), RecordingMetrics())
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        await hub.start()
        await door.start()
        credentials = tmp_path / 'client/credentials.json'
        try:
            await pair(credentials, f'http://127.0.0.1:{door.port}', pairing.create_code('https://mac.example.ts.net'), 'Reference')
            frames: list[dict[str, Any]] = []
            async with ReferenceClient(credentials, tmp_path / 'clips', display=frames.append) as client:
                accepted = await client.command('say_text', {'text': 'Hello'}, id='stable-say')
                await until(lambda: any(frame['type'] == 'reply_delta' for frame in frames))
                disconnected = asyncio.create_task(client.reconnect())
                await until(lambda: not client.ready)
                release.set()
                await asyncio.wait_for(disconnected, 5)
                reply = await client.reply(accepted['request_id'])
                assert reply['payload']['text'] == 'One two.'
                assert await client.command('say_text', {'text': 'Hello'}, id='stable-say') == accepted
                deltas = [frame['payload']['text'] for frame in client.cache.frames() if frame['type'] == 'reply_delta']
                assert deltas == ['One ', 'two.']
                assert len([frame for frame in frames if frame['type'] == 'reply_delta']) == 2
                cursor = client.cache.cursor
            async with ReferenceClient(credentials, tmp_path / 'clips', display=frames.append) as resumed:
                assert resumed.cache.cursor >= cursor
                assert len([frame for frame in frames if frame['type'] == 'reply_delta']) == 2
        finally:
            release.set()
            await door.close()
            await hub.close()


async def test_upload_metrics_include_early_matching_clip_and_exact_body_retry(tmp_path: Path) -> None:
    import wave

    from aiohttp import web
    from tools.reference_client import ReferenceClient, save_credentials

    received = asyncio.Event()
    socket: web.WebSocketResponse | None = None
    uploads: list[tuple[str, str, bytes]] = []
    seq = 0

    async def event(kind: str, payload: dict[str, Any], pcm: bytes | None = None) -> None:
        nonlocal seq
        assert socket is not None
        seq += 1
        await socket.send_json({'version': 1, 'seq': seq, 'type': kind, 'client_id': 'reference',
                                'time': '2026-11-19T12:00:00Z', 'payload': payload})
        if pcm is not None:
            await socket.send_bytes(pcm)

    async def events(request: web.Request) -> web.WebSocketResponse:
        nonlocal socket
        assert request.headers['Authorization'] == 'Bearer fake-reference-token'
        socket = web.WebSocketResponse(protocols=['jarvis.v1'])
        await socket.prepare(request)
        await socket.send_json({'version': 1, 'type': 'replay_complete', 'through_seq': seq})
        async for message in socket:
            frame = json.loads(message.data)
            result = frame['payload']
            await socket.send_json({'version': 1, 'type': 'command_result', 'id': frame['id'], 'ok': True, 'result': result})
        return socket

    async def upload(request: web.Request) -> web.Response:
        assert request.headers['X-Jarvis-Audio-Format'] == 'pcm_s16le_16000_mono'
        assert request.headers['Content-Type'] == 'application/octet-stream'
        uploads.append((request.match_info['talk'], request.headers['Idempotency-Key'], await request.read()))
        if len(uploads) == 1:
            for speech_id, reply_id in (('news-speech', 'news-reply'), ('old-speech', 'old-reply'), ('matching-speech', 'matching-reply')):
                await event('speech_begin', {'speech_id': speech_id, 'reply_id': reply_id, 'sample_rate': 24000, 'channels': 1, 'sample_width': 2})
                await event('speech_clip', {'speech_id': speech_id, 'reply_id': reply_id, 'index': 0, 'text': 'Hello.', 'byte_length': 2}, b'\x01\x00')
            await event('reply_delta', {'reply_id': 'news-reply', 'request_id': None, 'text': 'News.'})
            await event('reply_delta', {'reply_id': 'old-reply', 'request_id': 'older-request', 'text': 'Old.'})
            await event('reply_delta', {'reply_id': 'matching-reply', 'request_id': 'matching-request', 'text': 'Hello.'})
            await asyncio.wait_for(received.wait(), 2)
            assert request.transport is not None
            request.transport.close()
            return web.Response()
        return web.json_response({'version': 1, 'type': 'command_result', 'id': uploads[0][1], 'ok': True,
                                  'result': {'talk_id': uploads[0][0], 'request_id': 'matching-request'}})

    app = web.Application()
    app.router.add_get('/v1/events', events)
    app.router.add_post('/v1/utterances/{talk}', upload)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = runner.addresses[0][1]
    credentials = tmp_path / 'client/credentials.json'
    save_credentials(credentials, {'origin': f'http://127.0.0.1:{port}', 'client_id': 'reference', 'device_token': 'fake-reference-token'})
    wav_path = tmp_path / 'talk.wav'
    with wave.open(str(wav_path), 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\x00\x80\xff\x7f')
    frames: list[dict[str, Any]] = []

    def display(frame: dict[str, Any]) -> None:
        frames.append(frame)
        if frame['type'] == 'saved_clip' and frame['reply_id'] == 'matching-reply':
            received.set()

    try:
        async with ReferenceClient(credentials, tmp_path / 'clips', display=display) as client:
            result, timing = await asyncio.wait_for(client.upload(wav_path), 5)
            assert result['request_id'] == 'matching-request'
            report = timing.report(client)
            assert len(uploads) == 2 and uploads[0] == uploads[1]
            assert uploads[0][2] == b'\x00\x80\xff\x7f'
            assert report['reply_id'] == 'matching-reply' and report['speech_id'] == 'matching-speech'
            assert report['body_handoff_end'] <= report['first_matching_clip_received'] < report['http_response_end']
            assert report['upload_duration_seconds'] == report['body_handoff_end'] - report['release_upload_start']
            assert report['handoff_to_first_clip_seconds'] == report['first_matching_clip_received'] - report['body_handoff_end']
            assert report['release_to_first_clip_seconds'] == report['first_matching_clip_received'] - report['release_upload_start']
    finally:
        await runner.cleanup()


async def test_refused_token_is_erased_without_disclosing_it(tmp_path: Path) -> None:
    from tools.reference_client import save_credentials

    with PairingStore(tmp_path / 'pairing.db') as pairing, EventLog(tmp_path / 'events.db') as log:
        hub = make_hub(FakeBackend(FakeClock()), EventOutput(log, RecordingOutput()), RecordingMetrics())
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        await door.start()
        credentials = tmp_path / 'client/credentials.json'
        save_credentials(credentials, {'origin': f'http://127.0.0.1:{door.port}', 'client_id': 'refused', 'device_token': 'fake-invalid-secret'})
        try:
            code, stdout, stderr = await invoke('--credentials', str(credentials), '--output', str(tmp_path / 'clips'), 'listen')
            assert code == 1 and 'pair again' in stderr
            assert not credentials.exists()
            assert 'fake-invalid-secret' not in stdout + stderr
        finally:
            await door.close()
            await hub.close()


async def test_executable_talk_rejects_wrong_format_empty_oversize_and_truncated(tmp_path: Path) -> None:
    import wave

    from tools.reference_client import save_credentials

    with PairingStore(tmp_path / 'pairing.db') as pairing, EventLog(tmp_path / 'events.db') as log:
        hub = make_hub(FakeBackend(FakeClock()), EventOutput(log, RecordingOutput()), RecordingMetrics())
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        await door.start()
        phone = pairing.exchange(pairing.create_code('https://mac.example.ts.net'), 'Reference', 'reference')
        credentials = tmp_path / 'client/credentials.json'
        save_credentials(credentials, {'origin': f'http://127.0.0.1:{door.port}', 'client_id': phone.client.id, 'device_token': phone.token})
        path = tmp_path / 'input.wav'
        cases = [(24000, 1, 2, b'\0' * 4, 'ffmpeg'), (16000, 2, 2, b'\0' * 4, 'ffmpeg'),
                 (16000, 1, 1, b'\0' * 4, 'ffmpeg'), (16000, 1, 2, b'', '300 seconds'),
                 (16000, 1, 2, b'\0' * 9_600_002, '300 seconds'), (16000, 1, 2, b'\0' * 4, 'truncated')]
        try:
            for rate, channels, width, data, error in cases:
                with wave.open(str(path), 'wb') as wav:
                    wav.setparams((channels, width, rate, 0, 'NONE', 'not compressed'))
                    wav.writeframes(data)
                if error == 'truncated':
                    path.write_bytes(path.read_bytes()[:-2])
                code, stdout, stderr = await invoke('--credentials', str(credentials), '--output', str(tmp_path / 'clips'),
                                                    'talk', str(path))
                assert code == 1 and error in stderr
                assert phone.token not in stdout + stderr
                assert not any(event.type == 'heard' for event in log.after(0))
            path.write_bytes(b'not a WAV')
            code, stdout, stderr = await invoke('--credentials', str(credentials), 'talk', str(path))
            assert code == 1 and 'ffmpeg' in stderr
            assert phone.token not in stdout + stderr
        finally:
            await door.close()
            await hub.close()


async def test_incomplete_clip_reconnects_before_header_and_malformed_binary_closes_1002(tmp_path: Path) -> None:
    import pytest
    from aiohttp import web
    from tools.reference_client import ReferenceClient, save_credentials

    for malformed in (False, True):
        afters: list[str] = []
        close_codes: list[int | None] = []

        async def events(request: web.Request) -> web.WebSocketResponse:
            afters.append(request.query['after'])
            socket = web.WebSocketResponse(protocols=['jarvis.v1'])
            await socket.prepare(request)
            await socket.send_json({'version': 1, 'type': 'replay_complete', 'through_seq': 0 if len(afters) == 1 else 2})
            presence = await socket.receive_json()
            await socket.send_json({'version': 1, 'type': 'command_result', 'id': presence['id'], 'ok': True, 'result': {'can_show_text': True}})
            if len(afters) == 1:
                await socket.send_json({'version': 1, 'seq': 1, 'type': 'speech_begin', 'client_id': 'reference',
                                        'payload': {'speech_id': 'speech', 'reply_id': 'reply', 'sample_rate': 24000, 'channels': 1, 'sample_width': 2}})
                await socket.send_json({'version': 1, 'seq': 2, 'type': 'speech_clip', 'client_id': 'reference',
                                        'payload': {'speech_id': 'speech', 'reply_id': 'reply', 'index': 0, 'byte_length': 4}})
                if malformed:
                    await socket.send_bytes(b'\0\0')
                    await socket.receive()
                    close_codes.append(socket.close_code)
                else:
                    await socket.close()
            else:
                async for _message in socket:
                    pass
            return socket

        app = web.Application()
        app.router.add_get('/v1/events', events)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, '127.0.0.1', 0).start()
        root = tmp_path / str(malformed)
        credentials = root / 'credentials.json'
        save_credentials(credentials, {'origin': f'http://127.0.0.1:{runner.addresses[0][1]}', 'client_id': 'reference', 'device_token': 'fake-token'})
        try:
            if malformed:
                with pytest.raises(ValueError, match='Malformed'):
                    async with ReferenceClient(credentials, root / 'clips', display=lambda _: None) as client:
                        await asyncio.wait_for(client.wait(lambda: False), 3)
                assert close_codes == [1002]
            else:
                async with ReferenceClient(credentials, root / 'clips', display=lambda _: None) as client:
                    await asyncio.wait_for(client.wait(lambda: len(afters) == 2 and client.ready), 3)
                    assert afters == ['0', '1']
                    assert client.cache.cursor == 2
                    assert client.clips == []
            assert list((root / 'clips').glob('*.wav')) == []
        finally:
            await runner.cleanup()


@pytest.mark.parametrize('early_audio', [False, True])
async def test_executable_talk_recovers_text_without_waiting_for_lost_live_audio(tmp_path: Path, early_audio: bool) -> None:
    import wave

    import aiohttp
    from jarvis_hub.voice.speech import Speech
    from tests.fakes import FakeTts, Gate, RecordingPlayer, until
    from tests.protocol_exchanges import connect, receive
    from tests.test_uploaded_talk import Transcriber
    from tools.reference_client import save_credentials

    release = asyncio.Event()
    wav_path = tmp_path / 'talk.wav'
    with wave.open(str(wav_path), 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\x00\x80\xff\x7f')
    clock, metrics = FakeClock(), RecordingMetrics()
    with PairingStore(tmp_path / 'pairing.db') as pairing, EventLog(tmp_path / 'events.db') as log:
        speech = Speech(FakeTts(), RecordingPlayer(), clock, metrics=metrics, notice=lambda _: None)
        opening = 'One sentence. ' if early_audio else 'One '
        backend = FakeBackend(clock, [[Text(opening), Gate(release), Text('two.')]])
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), metrics, speech=speech)
        transcriber = Transcriber()
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log, transcription=transcriber)
        speech.start()
        await hub.start()
        await door.start()
        phone = pairing.exchange(pairing.create_code('https://mac.example.ts.net'), 'Reference', 'reference')
        credentials = tmp_path / 'client/credentials.json'
        save_credentials(credentials, {'origin': f'http://127.0.0.1:{door.port}', 'client_id': phone.client.id, 'device_token': phone.token})
        pending = asyncio.create_task(invoke('--credentials', str(credentials), '--output', str(tmp_path / 'clips'),
                                             '--timeout', '4', 'talk', str(wav_path)))
        try:
            await until(lambda: any(event.type == 'reply_delta' for event in log.after(0)), timeout=3)
            if early_audio:
                await until(lambda: len(list((tmp_path / 'clips').glob('*.wav'))) == 1)
            async with aiohttp.ClientSession() as session, connect(session, door, phone.token) as replacement:
                while (await receive(replacement))['type'] != 'replay_complete':
                    pass
            release.set()
            await until(lambda: any(event.type == 'reply_end' for event in log.after(0)))
            code, stdout, stderr = await pending
            assert code == 0, stderr
            frames = [json.loads(line) for line in stdout.splitlines()]
            endings = [frame for frame in frames if frame['type'] == 'reply_end']
            assert len(endings) == 1 and endings[0]['payload']['text'] == opening + 'two.'
            report = next(frame for frame in frames if frame['type'] == 'upload_metrics')
            assert report['reply_id'] == endings[0]['payload']['reply_id']
            assert report['audio_status'] == ('partial' if early_audio else 'unavailable')
            assert report['audio_reason'] == 'disconnected'
            if early_audio:
                assert report['first_matching_clip_received'] is not None
                assert report['release_to_first_clip_seconds'] >= 0
            else:
                assert report['first_matching_clip_received'] is None
                assert report['release_to_first_clip_seconds'] is None
                assert report['handoff_to_first_clip_seconds'] is None
            assert transcriber.calls == 1
            assert len([event for event in log.after(0) if event.type == 'heard']) == 1
            assert phone.token not in stdout + stderr
        finally:
            release.set()
            if not pending.done():
                await pending
            await door.close()
            await hub.close()
            await speech.close()


@pytest.mark.parametrize('synthesis', ['late', 'failed'])
async def test_executable_talk_keeps_text_success_when_audio_is_late_or_missing(tmp_path: Path, synthesis: str) -> None:
    import wave

    from jarvis_hub.voice.speech import Speech
    from tests.fakes import FakeTts, RecordingPlayer, until
    from tests.test_uploaded_talk import Transcriber
    from tools.reference_client import save_credentials

    wav_path = tmp_path / 'talk.wav'
    with wave.open(str(wav_path), 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\x00\x80\xff\x7f')
    clock, metrics = FakeClock(), RecordingMetrics()
    tts = FakeTts(fail_on='First sentence.' if synthesis == 'failed' else None)
    tts.hold.clear()
    with PairingStore(tmp_path / 'pairing.db') as pairing, EventLog(tmp_path / 'events.db') as log:
        speech = Speech(tts, RecordingPlayer(), clock, metrics=metrics, notice=lambda _: None)
        text = 'First sentence.' if synthesis == 'failed' else 'First sentence. Second sentence.'
        backend = FakeBackend(clock, [[Text(text)]])
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), metrics, speech=speech)
        transcriber = Transcriber()
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log, transcription=transcriber)
        speech.start()
        await hub.start()
        await door.start()
        phone = pairing.exchange(pairing.create_code('https://mac.example.ts.net'), 'Reference', 'reference')
        credentials = tmp_path / 'client/credentials.json'
        save_credentials(credentials, {'origin': f'http://127.0.0.1:{door.port}', 'client_id': phone.client.id, 'device_token': phone.token})
        pending = asyncio.create_task(invoke('--credentials', str(credentials), '--output', str(tmp_path / 'clips'),
                                             '--timeout', '2.5', 'talk', str(wav_path)))
        try:
            await until(lambda: any(event.type == 'reply_end' for event in log.after(0)), timeout=2)
            await asyncio.sleep(0.15)
            assert not pending.done()
            tts.hold.set()
            code, stdout, stderr = await pending
            assert code == 0, stderr
            frames = [json.loads(line) for line in stdout.splitlines()]
            ending = next(frame for frame in frames if frame['type'] == 'reply_end')
            report = next(frame for frame in frames if frame['type'] == 'upload_metrics')
            assert report['reply_id'] == ending['payload']['reply_id']
            assert ending['payload']['text'] == text
            if synthesis == 'late':
                assert report['audio_status'] == 'complete' and report['audio_reason'] is None
                assert report['first_matching_clip_received'] is not None
                assert len([frame for frame in frames if frame['type'] == 'saved_clip']) == 2
            else:
                assert report['audio_status'] == 'unavailable' and report['audio_reason'] == 'deadline'
                assert report['first_matching_clip_received'] is None
                assert report['release_to_first_clip_seconds'] is None
                assert report['handoff_to_first_clip_seconds'] is None
            assert transcriber.calls == 1
            assert phone.token not in stdout + stderr
        finally:
            tts.hold.set()
            if not pending.done():
                await pending
            await door.close()
            await hub.close()
            await speech.close()
