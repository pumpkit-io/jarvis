import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from jarvis_hub.hub import Line
from tests.fakes import FakeBackend, FakeClock, FakeTts, Gate, RecordingPlayer, Set, Text, Tool, until
from tests.protocol_exchanges import command, connect, receive
from tests.test_remote_speech import control, foreground, remote_rig, through


async def test_silence_still_delivers_spoken_text_and_replay_is_refused(tmp_path: Path) -> None:
    tts = FakeTts()
    async with remote_rig(tmp_path, tts=tts) as (session, door, hub, _log, _backend, phones, _pairing):
        async with connect(session, door, phones[0].token) as socket:
            await foreground(socket)
            result = await control(socket, 'off', 'set_silence', silence=True)
            assert result['ok'] and result['result'] == {'silence': True}
            assert await control(socket, 'off', 'set_silence', silence=True) == result
            await hub.admit_line(Line('Hello', 0, typed=False, client_id=phones[0].client.id))
            frames = await through(socket, 'reply_end')
            assert not any(frame['type'].startswith('speech_') for frame in frames)
            reply = frames[-1]['payload']
            assert reply['text'] == 'First sentence. Second sentence.'
            rejected = await control(socket, 'play', 'replay_speech', reply_id=reply['reply_id'])
            assert rejected['error']['code'] == 'silenced'
            assert not any(text.startswith('First sentence.') for text, _ in tts.calls)


async def test_replay_preserves_active_reply_ingress_and_focus(tmp_path: Path) -> None:
    gate = asyncio.Event()
    backend = FakeBackend(FakeClock(), [[Text('Retained reply.')], [Text('Current opening. '), Gate(gate), Text('Current ending.')]])
    async with remote_rig(tmp_path, backend) as (session, door, hub, log, backend, phones, _pairing):
        async with connect(session, door, phones[0].token) as socket:
            await foreground(socket)
            await hub.admit_line(Line('Old', 0, typed=False, client_id=phones[0].client.id))
            old = await through(socket, 'speech_end')
            reply_id = next(f['payload']['reply_id'] for f in old if f['type'] == 'speech_begin')
            await hub.admit_line(Line('Current', 0, typed=False, client_id=phones[0].client.id))
            current = await through(socket, 'speech_clip')
            current_reply = next(f['payload']['reply_id'] for f in current if f['type'] == 'speech_begin')
            hub.clients.take_focus('terminal')
            await socket.send_json(command('replay', 'replay_speech', reply_id=reply_id))
            replay = await through(socket, 'speech_end')
            assert [f['payload']['text'] for f in replay if f['type'] == 'speech_clip'] == ['Retained reply.']
            result = next(f for f in replay if f.get('id') == 'replay')
            replay_id = result['result']['speech_id']
            assert next(f['payload']['speech_id'] for f in replay if f['type'] == 'speech_begin') == replay_id
            assert await control(socket, 'replay', 'replay_speech', reply_id=reply_id) == result
            interrupted = await control(socket, 'stop-replay', 'interrupt', speech_id=replay_id, last_clip_index=-1)
            assert interrupted['ok']
            gate.set()
            resumed = await through(socket, 'speech_end')
            assert [f['payload']['text'] for f in resumed if f['type'] == 'speech_clip'] == ['Current ending.']
            resumed_begin = next(f['payload'] for f in resumed if f['type'] == 'speech_begin')
            assert resumed_begin['reply_id'] == current_reply
            assert resumed_begin['speech_id'] != next(f['payload']['speech_id'] for f in current if f['type'] == 'speech_begin')
            stale = await control(socket, 'stale', 'playback_report', speech_id=replay_id, clip_index=0, state='finished')
            assert stale['error']['code'] == 'invalid_payload'
            assert hub.clients.focus == 'terminal'
            replies = [e for e in log.after(0) if e.type == 'reply_end']
            assert [e.payload['text'] for e in replies] == ['Retained reply.', 'Current opening. Current ending.']
            assert len(backend.sessions[0].received) == 3
            missing = await control(socket, 'missing', 'replay_speech', reply_id='unknown')
            assert missing['error']['code'] == 'reply_not_found'


async def test_silence_stops_only_destination_while_model_text_continues(tmp_path: Path) -> None:
    gate = asyncio.Event()
    tts, local = FakeTts(), RecordingPlayer()
    backend = FakeBackend(FakeClock(), [[Text('Opening. '), Gate(gate), Text('Ending.')], [Text('Mac reply.')]])
    async with remote_rig(tmp_path, backend, tts=tts, local=local) as (session, door, hub, _log, _backend, phones, _pairing):
        async with connect(session, door, phones[0].token) as socket:
            await foreground(socket)
            await hub.admit_line(Line('Phone', 0, typed=False, client_id=phones[0].client.id))
            await through(socket, 'speech_clip')
            await socket.send_json(command('off', 'set_silence', silence=True))
            stopped = await through(socket, 'command_result')
            assert stopped[-1]['ok']
            assert next(frame['payload']['reason'] for frame in stopped if frame['type'] == 'speech_stop') == 'silence'
            assert not hub.clients.remote_activity
            gate.set()
            frames = await through(socket, 'reply_end')
            assert frames[-1]['payload']['text'] == 'Opening. Ending.'
            assert frames[-1]['payload']['reason'] == 'complete'
            assert not any(frame['type'] == 'speech_clip' for frame in frames)
            assert 'Ending.' not in [text for text, _ in tts.calls]
            await hub.admit_line(Line('Mac', 0, typed=False))
            await through(socket, 'reply_end')
            await until(lambda: local.texts == ['Mac reply.'])


@pytest.mark.parametrize("in_process", [False, True])
async def test_silence_survives_reconnect_and_hub_restart_before_presence(tmp_path: Path, in_process: bool) -> None:
    async with remote_rig(tmp_path) as (session, door, hub, _log, _backend, phones, _pairing):
        token = phones[0].token
        async with connect(session, door, token) as socket:
            await foreground(socket)
            if in_process:
                client = hub.clients.get(phones[0].client.id)
                assert client is not None
                hub.attach_client(replace(client, silence=True))
            else:
                assert (await control(socket, 'off', 'set_silence', silence=True))['ok']
        async with connect(session, door, token) as socket:
            await foreground(socket)
            assert (await control(socket, 'reject', 'replay_speech', reply_id='anything'))['error']['code'] == 'silenced'
    async with remote_rig(tmp_path) as (session, door, hub, _log, _backend, _phones, _pairing):
        async with connect(session, door, token) as socket:
            await foreground(socket)
            assert (await control(socket, 'reject-restart', 'replay_speech', reply_id='anything'))['error']['code'] == 'silenced'
            client = hub.clients.get(token.split('.')[0])
            assert client is not None and client.silence


async def test_replay_is_private_retains_language_and_replaces_prior_stream(tmp_path: Path) -> None:
    tts = FakeTts()
    backend = FakeBackend(FakeClock(), [[Text('Buonasera, signore.')]])
    async with remote_rig(tmp_path, backend, tts=tts) as (session, door, hub, _log, _backend, phones, _pairing):
        async with connect(session, door, phones[0].token) as phone, connect(session, door, phones[1].token) as observer:
            await foreground(phone)
            await foreground(observer)
            await hub.admit_line(Line('Ciao', 0, typed=False, client_id=phones[0].client.id, lang='it'))
            original = await through(phone, 'speech_end')
            reply_id = next(f['payload']['reply_id'] for f in original if f['type'] == 'speech_begin')
            await through(observer, 'reply_end')
            hub.clients.take_focus('terminal')
            await phone.send_json(command('play1', 'replay_speech', reply_id=reply_id))
            first = await through(phone, 'speech_end')
            first_id = next(f['payload']['speech_id'] for f in first if f['type'] == 'speech_begin')
            await phone.send_json(command('play2', 'replay_speech', reply_id=reply_id))
            second = await through(phone, 'speech_end')
            assert any(f['type'] == 'speech_stop' and f['payload']['speech_id'] == first_id for f in second)
            assert next(f['payload']['speech_id'] for f in second if f['type'] == 'speech_begin') != first_id
            assert len([call for call in tts.calls if call == ('Buonasera, signore.', 'im_nicola')]) == 4
            await observer.send_json(command('barrier', 'set_presence', can_show_text=True))
            while (frame := await receive(observer)).get('id') != 'barrier':
                assert not frame['type'].startswith('speech_')
            assert hub.clients.focus == 'terminal'


async def test_silence_on_phone_leaves_active_mac_reply_untouched(tmp_path: Path) -> None:
    gate = asyncio.Event()
    local = RecordingPlayer()
    backend = FakeBackend(FakeClock(), [[Text('Mac opening. '), Gate(gate), Text('Mac ending.')]])
    async with remote_rig(tmp_path, backend, local=local) as (session, door, hub, _log, _backend, phones, _pairing):
        async with connect(session, door, phones[0].token) as socket:
            await foreground(socket)
            await hub.admit_line(Line('Mac', 0, typed=False))
            await until(lambda: local.texts == ['Mac opening.'])
            stops = local.events.count('stop')
            assert (await control(socket, 'off', 'set_silence', silence=True))['ok']
            gate.set()
            await through(socket, 'reply_end')
            await until(lambda: local.texts == ['Mac opening.', 'Mac ending.'])
            assert local.events.count('stop') == stops


async def test_reconnected_silent_focus_tells_held_news_as_text_once(tmp_path: Path) -> None:
    arrival, reported = asyncio.Event(), asyncio.Event()
    tts = FakeTts()
    backend = FakeBackend(FakeClock(), [[Tool('delegate', {'request': 'Fix typo', 'context': ''})],
                                      [Text('Two updates, sir.')]],
                          orchestrator=[[Gate(arrival), Tool('report', {'task_id': 'T1', 'spoken': 'First'}),
                                         Tool('report', {'task_id': 'T1', 'spoken': 'Second'}), Set(reported)]])
    async with remote_rig(tmp_path, backend, tts=tts) as (session, door, hub, _log, _backend, phones, _pairing):
        token = phones[0].token
        async with connect(session, door, token) as socket:
            await foreground(socket)
            assert (await control(socket, 'off', 'set_silence', silence=True))['ok']
            assert (await control(socket, 'task', 'say_text', text='Fix typo'))['ok']
            await until(lambda: len(backend.all_sessions('orchestrator')) == 1)
        await until(lambda: (client := hub.clients.get(phones[0].client.id)) is not None and not client.connected)
        arrival.set()
        await reported.wait()
        async with connect(session, door, token) as socket:
            await foreground(socket)
            frames = await through(socket, 'reply_end')
            assert frames[-1]['payload']['text'] == 'Two updates, sir.'
            assert not any(frame['type'].startswith('speech_') for frame in frames)
            message = backend.session('talker').received[-1]
            assert message.startswith('<event source="hub" away="2">')
            assert message.count('task="T1"') == 2
            assert not any(text == 'Two updates, sir.' for text, _ in tts.calls)
