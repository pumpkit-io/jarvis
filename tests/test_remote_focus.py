import asyncio
import threading
from pathlib import Path
from typing import Literal

import pytest

from jarvis_hub.voice.player import Samples
from jarvis_hub.hub import Line
from tests.fakes import AwaitInterrupt, FakeBackend, FakeClock, FakeTts, Gate, RecordingPlayer, Set, Text, Tool, until
from tests.protocol_exchanges import command, connect, receive
from tests.test_remote_speech import control, foreground, remote_rig, through


async def test_take_focus_broadcasts_without_turn_or_ending_quiet_and_retry_is_durable(tmp_path: Path) -> None:
    backend = FakeBackend(FakeClock(), [[Tool('quiet_mode', {'on': True})]])
    async with remote_rig(tmp_path, backend) as (session, door, hub, log, backend, phones, _pairing):
        async with connect(session, door, phones[0].token) as one, connect(session, door, phones[1].token) as two:
            await foreground(one)
            await foreground(two)
            await hub.admit_line(Line('Quiet please', 0, client_id=phones[1].client.id))
            await through(one, 'reply_end')
            baseline = len(backend.session('talker').received)
            await one.send_json(command('focus', 'take_focus'))
            frames = await through(one, 'command_result')
            result = frames[-1]
            assert result['ok'] and result['result'] == {'focus_client_id': phones[0].client.id}
            assert any(f['type'] == 'status' and f['payload']['focus_client_id'] == phones[0].client.id and f['payload']['quiet'] for f in frames)
            while True:
                frame = await receive(two)
                if frame['type'] == 'status' and frame['payload']['focus_client_id'] == phones[0].client.id:
                    assert frame['payload']['quiet']
                    break
            assert len(backend.session('talker').received) == baseline
            assert (await control(two, 'other', 'take_focus'))['ok']
            assert await control(one, 'focus', 'take_focus') == result
            assert hub.clients.focus == phones[1].client.id
            assert not (await control(one, 'bad', 'take_focus', client_id=phones[1].client.id))['ok']
        async with connect(session, door, phones[0].token, after=log.high_water) as reconnect:
            await foreground(reconnect)
            assert await control(reconnect, 'focus', 'take_focus') == result
            assert hub.clients.focus == phones[1].client.id


async def test_unheard_replay_on_other_client_does_not_hold_focused_idle_news(tmp_path: Path) -> None:
    arrival, reported = asyncio.Event(), asyncio.Event()
    backend = FakeBackend(FakeClock(), [[Tool('delegate', {'request': 'Fix typo', 'context': ''}), Text('Working, sir.')],
                                       [Text('The fix is ready.')]],
                          orchestrator=[[Gate(arrival), Tool('report', {'task_id': 'T1', 'spoken': 'Ready'}), Set(reported)]])
    async with remote_rig(tmp_path, backend) as (session, door, hub, _log, backend, phones, _pairing):
        async with connect(session, door, phones[0].token) as one, connect(session, door, phones[1].token) as two:
            await foreground(one)
            await foreground(two)
            await hub.admit_line(Line('Fix typo', 0, client_id=phones[0].client.id))
            reply = (await through(one, 'reply_end'))[-1]['payload']['reply_id']
            await one.send_json(command('replay', 'replay_speech', reply_id=reply))
            await through(one, 'speech_end')
            assert hub.clients.remote_activity
            assert (await control(two, 'focus', 'take_focus'))['ok']
            arrival.set()
            await reported.wait()
            frames = await through(two, 'speech_end')
            assert [f['payload']['text'] for f in frames if f['type'] == 'speech_clip'] == ['The fix is ready.']
            assert hub.clients.remote_activity
            shared = await through(one, 'reply_end')
            assert shared[-1]['payload']['text'] == 'The fix is ready.'
            assert not any(f['type'].startswith('speech_') for f in shared)


async def test_news_text_starts_while_another_clients_replay_is_still_synthesizing(tmp_path: Path) -> None:
    entered = threading.Event()

    class GatedTts(FakeTts):
        def synthesize(self, text: str, voice: str) -> Samples:
            if text == 'Working, sir.':
                entered.set()
            return super().synthesize(text, voice)

    arrival, reported = asyncio.Event(), asyncio.Event()
    tts = GatedTts()
    backend = FakeBackend(FakeClock(), [[Tool('delegate', {'request': 'Fix typo', 'context': ''}), Text('Working, sir.')],
                                       [Text('The fix is ready.')]],
                          orchestrator=[[Gate(arrival), Tool('report', {'task_id': 'T1', 'spoken': 'Ready'}), Set(reported)]])
    async with remote_rig(tmp_path, backend, tts=tts) as (session, door, hub, _log, backend, phones, _pairing):
        try:
            async with connect(session, door, phones[0].token) as one, connect(session, door, phones[1].token) as two:
                await foreground(one)
                await foreground(two)
                await hub.admit_line(Line('Fix typo', 0, client_id=phones[0].client.id))
                reply = (await through(one, 'reply_end'))[-1]['payload']['reply_id']
                await through(two, 'reply_end')
                await until(lambda: len(tts.calls) == 2)
                tts.hold.clear()
                assert (await control(one, 'replay-held', 'replay_speech', reply_id=reply))['ok']
                await until(entered.is_set)
                assert (await control(two, 'focus-held', 'take_focus'))['ok']
                arrival.set()
                await reported.wait()
                async with asyncio.timeout(1):
                    frames = await through(two, 'reply_end')
                    shared = await through(one, 'reply_end')
                assert frames[-1]['payload']['text'] == shared[-1]['payload']['text'] == 'The fix is ready.'
                assert len(backend.session('talker').received) == 3
                assert not tts.hold.is_set()
                assert not any(f['type'].startswith('speech_') for f in frames + shared)
                tts.hold.set()
                audio = await through(two, 'speech_end')
                assert [f['payload']['text'] for f in audio if f['type'] == 'speech_clip'] == ['The fix is ready.']
                assert hub.clients.remote_activity
        finally:
            tts.hold.set()


async def test_focused_pending_clip_defers_news_until_finished_not_speech_end(tmp_path: Path) -> None:
    arrival, reported = asyncio.Event(), asyncio.Event()
    backend = FakeBackend(FakeClock(), [[Tool('delegate', {'request': 'Fix typo', 'context': ''}), Text('Working, sir.')],
                                       [Text('The fix is ready.')]],
                          orchestrator=[[Gate(arrival), Tool('report', {'task_id': 'T1', 'spoken': 'Ready'}), Set(reported)]])
    async with remote_rig(tmp_path, backend) as (session, door, hub, _log, backend, phones, _pairing):
        async with connect(session, door, phones[0].token) as socket:
            await foreground(socket)
            await hub.admit_line(Line('Fix typo', 0, typed=False, client_id=phones[0].client.id))
            original = await through(socket, 'speech_end')
            stream = next(f['payload']['speech_id'] for f in original if f['type'] == 'speech_begin')
            arrival.set()
            await reported.wait()
            await asyncio.sleep(.01)
            assert len(backend.session('talker').received) == 2
            assert (await control(socket, 'finished', 'playback_report', speech_id=stream, clip_index=0, state='finished'))['ok']
            frames = await through(socket, 'speech_end')
            assert [f['payload']['text'] for f in frames if f['type'] == 'speech_clip'] == ['The fix is ready.']


async def test_focused_talk_defers_news_without_generating_a_turn(tmp_path: Path) -> None:
    arrival, reported = asyncio.Event(), asyncio.Event()
    backend = FakeBackend(FakeClock(), [[Tool('delegate', {'request': 'Fix typo', 'context': ''})], [Text('The fix is ready.')]],
                          orchestrator=[[Gate(arrival), Tool('report', {'task_id': 'T1', 'spoken': 'Ready'}), Set(reported)]])
    async with remote_rig(tmp_path, backend) as (session, door, hub, _log, backend, phones, _pairing):
        async with connect(session, door, phones[0].token) as socket:
            await foreground(socket)
            await hub.admit_line(Line('Fix typo', 0, client_id=phones[0].client.id))
            await through(socket, 'reply_end')
            assert (await control(socket, 'begin', 'talk_begin', talk_id='talk'))['ok']
            arrival.set()
            await reported.wait()
            await asyncio.sleep(.01)
            assert len(backend.session('talker').received) == 2
            assert (await control(socket, 'cancel', 'talk_cancel', talk_id='talk'))['ok']
            frames = await through(socket, 'speech_end')
            assert [f['payload']['text'] for f in frames if f['type'] == 'speech_clip'] == ['The fix is ready.']


@pytest.mark.parametrize('silence', [False, True])
async def test_reconnect_batches_two_held_items_after_foreground_and_tells_once(tmp_path: Path, silence: bool) -> None:
    arrival, reported = asyncio.Event(), asyncio.Event()
    local = RecordingPlayer()
    backend = FakeBackend(FakeClock(), [[Tool('delegate', {'request': 'Fix typo', 'context': ''})],
                                       [Text('Two updates, sir.')], [Text('Nothing repeated.')]],
                          orchestrator=[[Gate(arrival), Tool('report', {'task_id': 'T1', 'spoken': 'First'}),
                                         Tool('report', {'task_id': 'T1', 'spoken': 'Second'}), Set(reported)]])
    async with remote_rig(tmp_path, backend, local=local) as (session, door, hub, log, backend, phones, _pairing):
        async with connect(session, door, phones[0].token) as socket:
            await foreground(socket)
            await hub.admit_line(Line('Fix typo', 0, client_id=phones[0].client.id))
            await through(socket, 'reply_end')
            assert (await control(socket, 'silence', 'set_silence', silence=silence))['ok']
        await until(lambda: (client := hub.clients.get(phones[0].client.id)) is not None and not client.connected)
        arrival.set()
        await reported.wait()
        await asyncio.sleep(.01)
        assert len(backend.session('talker').received) == 2
        assert local.texts == []
        async with connect(session, door, phones[0].token, after=log.high_water) as reconnect:
            await through(reconnect, 'replay_complete')
            await asyncio.sleep(.01)
            assert len(backend.session('talker').received) == 2
            await reconnect.send_json(command('foreground', 'set_presence', can_show_text=True))
            frames = await through(reconnect, 'reply_end')
            if silence:
                assert not any(f['type'].startswith('speech_') for f in frames)
            assert frames[-1]['payload']['text'] == 'Two updates, sir.'
            if not silence:
                audio = await through(reconnect, 'speech_end')
                stream = next(f['payload']['speech_id'] for f in audio if f['type'] == 'speech_begin')
                assert [f['payload']['text'] for f in audio if f['type'] == 'speech_clip'] == ['Two updates, sir.']
                assert (await control(reconnect, 'finished', 'playback_report', speech_id=stream, clip_index=0, state='finished'))['ok']
            message = backend.session('talker').received[-1]
            assert message.startswith('<event source="hub" away="2">')
            assert message.count('task="T1"') == 2
            await control(reconnect, 'background', 'set_presence', can_show_text=False)
            await control(reconnect, 'foreground-again', 'set_presence', can_show_text=True)
            await hub.admit_line(Line('Hello', 0, client_id=phones[0].client.id))
            await through(reconnect, 'reply_end')
            assert backend.session('talker').received[-1].startswith('<user')
            assert local.texts == []


async def test_replaced_socket_cannot_take_focus_after_waiting_for_admission(tmp_path: Path) -> None:
    class InterruptGate(asyncio.Event):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()

        async def wait(self) -> Literal[True]:
            self.entered.set()
            return await super().wait()

    gate, attempted = InterruptGate(), asyncio.Event()
    backend = FakeBackend(FakeClock(), [[Text('Opening. '), AwaitInterrupt()], [Text('Current reply.')]], interrupt_gate=gate)
    async with remote_rig(tmp_path, backend, focus_entered=attempted) as (session, door, hub, _log, _backend, phones, _pairing):
        async with connect(session, door, phones[0].token) as one, connect(session, door, phones[1].token) as two:
            await foreground(one)
            await foreground(two)
            await hub.admit_line(Line('Opening', 0, typed=False, client_id=phones[0].client.id))
            await through(one, 'speech_clip')
            blocker = asyncio.create_task(hub.admit_line(Line('Current', 0, client_id=phones[1].client.id)))
            try:
                await gate.entered.wait()
                await one.send_json(command('obsolete', 'take_focus'))
                await attempted.wait()
                async with connect(session, door, phones[0].token) as replacement:
                    await foreground(replacement)
                    gate.set()
                    await blocker
                    assert not (await control(replacement, 'obsolete', 'take_focus'))['ok']
                    assert hub.clients.focus == phones[1].client.id
            finally:
                gate.set()
                await blocker


async def test_first_input_claims_two_held_items_once_while_focus_is_away(tmp_path: Path) -> None:
    arrival, reported = asyncio.Event(), asyncio.Event()
    local = RecordingPlayer()
    backend = FakeBackend(FakeClock(), [[Tool('delegate', {'request': 'Fix typo', 'context': ''})],
                                       [Text('Two updates, sir.')], [Text('Anything else?')]],
                          orchestrator=[[Gate(arrival), Tool('report', {'task_id': 'T1', 'spoken': 'First'}),
                                         Tool('report', {'task_id': 'T1', 'spoken': 'Second'}), Set(reported)]])
    async with remote_rig(tmp_path, backend, local=local) as (session, door, hub, _log, backend, phones, _pairing):
        async with connect(session, door, phones[0].token) as one, connect(session, door, phones[1].token) as two:
            await foreground(one)
            await foreground(two)
            await hub.admit_line(Line('Fix typo', 0, client_id=phones[0].client.id))
            await through(one, 'reply_end')
            await through(two, 'reply_end')
            await one.close()
            await until(lambda: (client := hub.clients.get(phones[0].client.id)) is not None and not client.connected)
            arrival.set()
            await reported.wait()
            assert local.texts == []
            await hub.admit_line(Line("What's new?", 0, typed=False, client_id=phones[1].client.id))
            frames = await through(two, 'reply_end')
            assert frames[-1]['payload']['text'] == 'Two updates, sir.'
            message = backend.session('talker').received[-1]
            assert message.startswith('<event source="hub" away="2">') and "What's new?" in message
            assert message.count('task="T1"') == 2
            audio = await through(two, 'speech_end')
            stream = next(f['payload']['speech_id'] for f in audio if f['type'] == 'speech_begin')
            assert (await control(two, 'heard', 'playback_report', speech_id=stream, clip_index=0, state='finished'))['ok']
            await two.send_json(command('second', 'say_text', text='Hello'))
            await through(two, 'reply_end')
            assert backend.session('talker').received[-1].startswith('<user')
            assert local.texts == []
