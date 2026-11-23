import asyncio
import uuid
import weakref
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import aiohttp

from jarvis_hub.door import Door
from jarvis_hub.event_log import EventLog, EventOutput
from jarvis_hub.hub import Hub, Line
from jarvis_hub.pairing import PairingResult, PairingStore
from jarvis_hub.voice.live import LiveInputs
from jarvis_hub.voice.speech import Speech
from jarvis_hub.voice.voice_mode import VoiceMode
from jarvis_hub.voice.player import Samples
from tests.fakes import FakeBackend, FakeClock, FakeTts, RecordingMetrics, RecordingOutput, RecordingPlayer, Text, Tool, make_hub, until
from tests.protocol_exchanges import DelayedAdmission, command, connect, receive


async def test_spoken_reply_has_ordered_private_pcm_and_reported_playback(tmp_path: Path) -> None:
    clock, metrics, local = FakeClock(), RecordingMetrics(), RecordingPlayer()
    class Tts(FakeTts):
        notice: Callable[[str], None] = lambda _: None
        samples: list[weakref.ReferenceType[Samples]]
        def __init__(self) -> None:
            super().__init__()
            self.samples = []
        def synthesize(self, text: str, voice: str) -> Samples:
            samples = super().synthesize(text, voice)
            self.samples.append(weakref.ref(samples))
            self.notice('Worker synthesis notice')
            return samples

    tts = Tts()
    with EventLog(tmp_path / 'events.db') as log, PairingStore(tmp_path / 'pairing.db') as pairing:
        paired = pairing.exchange(pairing.create_code('https://mac.example.ts.net'), 'Phone', 'iphone')
        output = EventOutput(log, RecordingOutput())
        tts.notice = output.notice
        speech = Speech(tts, local, clock, metrics=metrics, notice=output.notice)
        hub = make_hub(FakeBackend(clock, [[Text('First sentence. Second sentence.')]]), output, metrics, speech=speech)
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        speech.start()
        await hub.start()
        await door.start()
        try:
            async with aiohttp.ClientSession() as session, connect(session, door, paired.token) as socket:
                while (await receive(socket))['type'] != 'replay_complete':
                    pass
                await socket.send_json(command('presence', 'set_presence', can_show_text=True))
                while (await receive(socket)).get('id') != 'presence':
                    pass
                await hub.admit_line(Line('Hello', clock.now(), typed=False, client_id=paired.client.id))
                frames: list[dict[str, Any]] = []
                pcm: list[bytes] = []
                while True:
                    frame = await receive(socket)
                    frames.append(frame)
                    if frame['type'] == 'speech_clip':
                        binary = await socket.receive()
                        assert binary.type == aiohttp.WSMsgType.BINARY
                        assert len(binary.data) == frame['payload']['byte_length']
                        pcm.append(binary.data)
                    if frame['type'] == 'speech_end':
                        break
                assert [f['type'] for f in frames if f['type'].startswith('speech_')] == ['speech_begin', 'speech_clip', 'speech_clip', 'speech_end']
                assert [f['seq'] for f in frames] == sorted(f['seq'] for f in frames)
                begin = next(f for f in frames if f['type'] == 'speech_begin')
                reply = next(f for f in frames if f['type'] == 'reply_delta')
                assert begin['payload']['reply_id'] == reply['payload']['reply_id']
                assert all(f['client_id'] == paired.client.id for f in frames if f['type'].startswith('speech_'))
                assert pcm == [b'\xcc\x0c' * (len(text) * 1200) for text in ('First sentence.', 'Second sentence.')]
                await until(lambda: all(reference() is None for reference in tts.samples))
                assert local.texts == []
                assert not any(e.type.startswith('speech_') for e in log.after(0))
        finally:
            await door.close()
            await hub.close()
            await speech.close()


@asynccontextmanager
async def remote_rig(tmp_path: Path, backend: FakeBackend | None = None, voice: VoiceMode | None = None, begin_entered: asyncio.Event | None = None, tts: FakeTts | None = None, local: RecordingPlayer | None = None, focus_entered: asyncio.Event | None = None, live: Callable[[VoiceMode], LiveInputs] | None = None) -> AsyncGenerator[tuple[aiohttp.ClientSession, Door, Hub, EventLog, FakeBackend, list[PairingResult], PairingStore]]:
    clock, metrics = FakeClock(), RecordingMetrics()
    with EventLog(tmp_path / 'events.db') as log, PairingStore(tmp_path / 'pairing.db') as pairing:
        phones = [pairing.exchange(pairing.create_code('https://mac.example.ts.net'), name, 'iphone') for name in ('One', 'Two')]
        output = EventOutput(log, RecordingOutput())
        speech = voice.speech if voice is not None else Speech(tts or FakeTts(), local or RecordingPlayer(), clock, metrics=metrics, notice=output.notice)
        backend = backend or FakeBackend(clock, [[Text('First sentence. Second sentence.')], [Text('New reply.')], [Text('Third reply.')]])
        hub = make_hub(backend, output, metrics, speech=speech, floor=voice,
                       on_stand_by=voice.stand_by if voice is not None else None,
                       on_end_conversation=voice.end_conversation if voice is not None else None)
        adapter = DelayedAdmission(hub)
        adapter.release.set()
        if focus_entered is not None:
            adapter.focus_entered = focus_entered
        if begin_entered is not None:
            adapter.begin_entered = begin_entered
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=adapter, log=log, revocation_interval=.005,
                    live=live(voice) if live is not None and voice is not None else None)
        if voice is not None:
            voice.start()
        else:
            assert isinstance(speech, Speech)
            speech.start()
        await hub.start()
        await door.start()
        try:
            async with aiohttp.ClientSession() as session:
                yield session, door, hub, log, backend, phones, pairing
        finally:
            await door.close()
            await hub.close()
            if voice is not None:
                await voice.close()
            else:
                assert isinstance(speech, Speech)
                await speech.close()


async def foreground(socket: aiohttp.ClientWebSocketResponse) -> None:
    while (await receive(socket))['type'] != 'replay_complete':
        pass
    presence = uuid.uuid4().hex
    await socket.send_json(command(presence, 'set_presence', can_show_text=True))
    while (await receive(socket)).get('id') != presence:
        pass


async def through(socket: aiohttp.ClientWebSocketResponse, kind: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    while True:
        frame = await receive(socket)
        frames.append(frame)
        if frame['type'] == 'speech_clip':
            assert (await socket.receive()).type == aiohttp.WSMsgType.BINARY
        if frame['type'] == kind:
            return frames


async def control(socket: aiohttp.ClientWebSocketResponse, id: str, kind: str, **payload: Any) -> dict[str, Any]:
    await socket.send_json(command(id, kind, **payload))
    while (frame := await receive(socket)).get('id') != id:
        if frame['type'] == 'speech_clip':
            await socket.receive()
    return frame


async def test_reports_interrupt_heard_text_and_private_routing(tmp_path: Path) -> None:
    async with remote_rig(tmp_path) as (session, door, hub, log, backend, phones, _pairing):
        async with connect(session, door, phones[0].token) as one, connect(session, door, phones[1].token) as two:
            await foreground(one)
            await foreground(two)
            await hub.admit_line(Line('First', 0, typed=False, client_id=phones[0].client.id))
            frames = await through(one, 'speech_end')
            stream = next(f['payload']['speech_id'] for f in frames if f['type'] == 'speech_begin')
            assert hub.clients.remote_activity
            second = await through(two, 'reply_end')
            assert not any(f['type'].startswith('speech_') for f in second)
            assert not (await control(two, 'foreign', 'interrupt', speech_id=stream, last_clip_index=0))['ok']
            assert not (await control(one, 'skip', 'playback_report', speech_id=stream, clip_index=1, state='finished'))['ok']
            assert (await control(one, 'first', 'playback_report', speech_id=stream, clip_index=0, state='finished'))['ok']
            assert (await control(one, 'stop', 'interrupt', speech_id=stream, last_clip_index=0))['ok']
            assert not hub.clients.remote_activity
            assert not (await control(one, 'late', 'playback_report', speech_id=stream, clip_index=1, state='finished'))['ok']
            await hub.admit_line(Line('Continue', 0, typed=False, client_id=phones[1].client.id))
            await through(two, 'speech_end')
            assert '<interrupted spoken="First sentence."/>' in backend.session('talker').received[-1]
            assert not any(e.type.startswith('speech_') for e in log.after(0))


async def test_a_talk_over_a_replay_carries_what_was_heard_after_a_reply_stood_by(tmp_path: Path) -> None:
    backend = FakeBackend(FakeClock(), [[Text('First sentence. Second sentence.')],
                                        [Text('Certainly. '), Tool('stand_by')], [Text('New reply.')]])
    async with remote_rig(tmp_path, backend) as (session, door, hub, _log, backend, phones, _pairing):
        async with connect(session, door, phones[0].token) as one:
            await foreground(one)
            await hub.admit_line(Line('First', 0, typed=False, client_id=phones[0].client.id))
            frames = await through(one, 'speech_end')
            reply = next(f['payload'] for f in frames if f['type'] == 'speech_begin')
            for index in (0, 1):
                await control(one, f'heard-{index}', 'playback_report', speech_id=reply['speech_id'],
                              clip_index=index, state='finished')
            await hub.admit_line(Line('On the Mac', 0, typed=False))
            await until(lambda: len(backend.session('talker').tool_results) == 1)
            replay = (await control(one, 'replay', 'replay_speech', reply_id=reply['reply_id']))['result']['speech_id']
            await through(one, 'speech_end')
            await control(one, 'replayed', 'playback_report', speech_id=replay, clip_index=0, state='finished')

            await hub.admit_line(Line('Continue', 0, typed=False, client_id=phones[0].client.id))

            await until(lambda: 'Continue' in backend.session('talker').received[-1])
            assert '<interrupted spoken="First sentence."/>' in backend.session('talker').received[-1]


async def test_stopping_the_last_clip_before_its_report_frees_the_mac_microphone(tmp_path: Path) -> None:
    async with remote_rig(tmp_path) as (session, door, hub, _log, _backend, phones, _pairing):
        async with connect(session, door, phones[0].token) as one:
            await foreground(one)
            await hub.admit_line(Line('First', 0, typed=False, client_id=phones[0].client.id))
            frames = await through(one, 'speech_end')
            stream = next(f['payload']['speech_id'] for f in frames if f['type'] == 'speech_begin')
            assert hub.clients.remote_activity
            assert (await control(one, 'stop', 'interrupt', speech_id=stream, last_clip_index=1))['ok']
            assert not hub.clients.remote_activity
            await hub.admit_line(Line('Continue', 0, typed=False, client_id=phones[0].client.id))
            after = await through(one, 'speech_begin')
            assert not any(f['type'] == 'speech_stop' for f in after)


async def test_replacement_and_revocation_retire_only_the_owned_speech(tmp_path: Path) -> None:
    async with remote_rig(tmp_path) as (session, door, hub, log, _backend, phones, pairing):
        async with connect(session, door, phones[0].token) as original:
            await foreground(original)
            await hub.admit_line(Line('First', 0, typed=False, client_id=phones[0].client.id))
            frames = await through(original, 'speech_end')
            old = next(f['payload']['speech_id'] for f in frames if f['type'] == 'speech_begin')
            async with connect(session, door, phones[0].token, after=log.high_water) as replacement:
                await foreground(replacement)
                assert not hub.clients.remote_activity
                assert not (await control(replacement, 'old', 'interrupt', speech_id=old, last_clip_index=0))['ok']
                await hub.admit_line(Line('Second', 0, typed=False, client_id=phones[0].client.id))
                fresh = await through(replacement, 'speech_end')
                stream = next(f['payload']['speech_id'] for f in fresh if f['type'] == 'speech_begin')
                assert stream != old and hub.clients.remote_activity
                assert (await control(replacement, 'finish', 'playback_report', speech_id=stream, clip_index=0, state='finished'))['ok']
                assert not hub.clients.remote_activity
                pairing.revoke('One')
                await until(lambda: (client := hub.clients.get(phones[0].client.id)) is not None and not client.connected)
                assert (await replacement.receive()).type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)


async def test_new_client_input_stops_prior_stream_and_background_can_only_drain(tmp_path: Path) -> None:
    async with remote_rig(tmp_path) as (session, door, hub, _log, _backend, phones, _pairing):
        async with connect(session, door, phones[0].token) as one, connect(session, door, phones[1].token) as two:
            await foreground(one)
            await foreground(two)
            await hub.admit_line(Line('First', 0, typed=False, client_id=phones[0].client.id))
            await through(one, 'speech_end')
            assert (await control(one, 'background', 'set_presence', can_show_text=False))['ok']
            assert hub.clients.remote_activity
            await hub.admit_line(Line('Next', 0, typed=False, client_id=phones[1].client.id))
            assert (await through(one, 'speech_stop'))[-1]['payload']['reason'] == 'interrupted'
            fresh = await through(two, 'speech_end')
            assert all(f['client_id'] == phones[1].client.id for f in fresh if f['type'].startswith('speech_'))


async def test_completed_client_stop_cannot_interrupt_another_clients_announcement(tmp_path: Path) -> None:
    import asyncio
    from tests.fakes import Gate, Tool

    announce = asyncio.Event()
    backend = FakeBackend(FakeClock(), [[Text('Complete reply.'), Tool('delegate', {'request': 'Fix typo', 'context': ''})],
                                       [Text('News on the second phone.')]],
                          orchestrator=[[Gate(announce), Tool('report', {'task_id': 'T1', 'spoken': 'Ready'})]])
    async with remote_rig(tmp_path, backend) as (session, door, hub, _log, _backend, phones, _pairing):
        async with connect(session, door, phones[0].token) as one, connect(session, door, phones[1].token) as two:
            await foreground(one)
            await foreground(two)
            await hub.admit_line(Line('Start', 0, typed=False, client_id=phones[0].client.id))
            first = await through(one, 'speech_end')
            old = next(f['payload']['speech_id'] for f in first if f['type'] == 'speech_begin')
            assert (await control(one, 'finish', 'playback_report', speech_id=old, clip_index=0, state='finished'))['ok']
            hub.clients.take_focus(phones[1].client.id)
            announce.set()
            second = await through(two, 'speech_end')
            fresh = next(f['payload']['speech_id'] for f in second if f['type'] == 'speech_begin')
            assert not (await control(one, 'late-stop', 'playback_report', speech_id=old, clip_index=0, state='stopped'))['ok']
            assert not (await control(one, 'late-interrupt', 'interrupt', speech_id=old, last_clip_index=0))['ok']
            assert hub.clients.remote_activity
            assert (await control(two, 'finish-current', 'playback_report', speech_id=fresh, clip_index=0, state='finished'))['ok']
            assert not hub.clients.remote_activity


async def test_queued_wire_pcm_mutes_mac_before_started_report_and_unmutes_at_drain(tmp_path: Path) -> None:
    import asyncio
    from jarvis_hub.voice.state import BargeIn, Timing
    from tests.fakes import ScriptedAudio

    clock, local, output = FakeClock(), RecordingPlayer(), RecordingOutput()
    audio = ScriptedAudio(clock)
    voice = VoiceMode(audio=audio, tts=FakeTts(), player=local, clock=clock, metrics=RecordingMetrics(),
                      notice=output.notice, timing=Timing(6, 4, 6, .5, tail_s=0))
    async with remote_rig(tmp_path, voice=voice) as (session, door, hub, _log, backend, phones, _pairing):
        running = asyncio.create_task(hub.run(voice.lines(hub.interrupt, hub.woke, hub.admit_spoken_revision)))
        try:
            async with connect(session, door, phones[0].token) as socket:
                await foreground(socket)
                await hub.admit_line(Line('Phone', 0, typed=False, client_id=phones[0].client.id))
                frames = await through(socket, 'speech_end')
                stream = next(f['payload']['speech_id'] for f in frames if f['type'] == 'speech_begin')
                received = list(backend.session('talker').received)
                audio.emit(BargeIn(clock.now(), clock.now()))
                audio.wake()
                audio.say('Echo from the phone')
                for _ in range(30):
                    await asyncio.sleep(0)
                assert backend.session('talker').received == received
                assert hub.clients.remote_activity
                for index in (0, 1):
                    assert (await control(socket, f'finish-{index}', 'playback_report', speech_id=stream, clip_index=index, state='finished'))['ok']
                assert not hub.clients.remote_activity
                audio.wake()
                await until(lambda: audio.capturing[-1:] == [True])
                audio.say('Fresh Mac request')
                await until(lambda: [text for text in local.texts if text] == ['New reply.'])
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


async def test_replaced_connection_cannot_begin_a_talk_after_waiting_for_admission(tmp_path: Path) -> None:
    from tests.fakes import AwaitInterrupt

    class InterruptGate(asyncio.Event):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
        async def wait(self) -> Literal[True]:
            self.entered.set()
            return await super().wait()

    gate, attempted = InterruptGate(), asyncio.Event()
    backend = FakeBackend(FakeClock(), [[Text('Opening sentence. '), AwaitInterrupt()], [Text('Current reply.')]], interrupt_gate=gate)
    async with remote_rig(tmp_path, backend, begin_entered=attempted) as (session, door, hub, _log, _backend, phones, _pairing):
        async with connect(session, door, phones[0].token) as one, connect(session, door, phones[1].token) as two:
            await foreground(one)
            await foreground(two)
            await hub.admit_line(Line('Opening', 0, typed=False, client_id=phones[0].client.id))
            await through(one, 'speech_clip')
            blocker = asyncio.create_task(hub.admit_line(Line('Current request', 0, typed=False, client_id=phones[1].client.id)))
            try:
                await gate.entered.wait()
                begin = command('obsolete-begin', 'talk_begin', talk_id='obsolete-talk')
                await one.send_json(begin)
                await attempted.wait()
                async with connect(session, door, phones[0].token) as replacement:
                    await foreground(replacement)
                    hub.clients.take_focus(phones[1].client.id)
                    gate.set()
                    await blocker
                    response = await control(replacement, 'obsolete-begin', 'talk_begin', talk_id='obsolete-talk')
                    assert response['ok'] is False
                    assert hub.clients.focus == phones[1].client.id
                    client = hub.clients.get(phones[0].client.id)
                    assert client is not None and not client.talking
            finally:
                gate.set()
                await blocker
