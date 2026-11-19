import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from jarvis_hub.clients import Client
from jarvis_hub.hub import Line
from jarvis_hub.voice.player import Clip, PlaybackReceipt, RemotePlayer, Samples, SoundPlayer
from jarvis_hub.voice.speech import Speech
from tests.fakes import FakeBackend, FakeClock, FakeTts, Gate, RecordingMetrics, RecordingOutput, RecordingPlayer, Set, Text, Tool, make_hub, until
from tests.test_client_news_serialization import Transport
from tests.test_player import FakeStream


@pytest.mark.parametrize("action", ["disconnect", "focus", "played", "silent"])
async def test_news_in_user_reply_keeps_its_requesting_client_and_delivery(action: str) -> None:
    clock, output, phone, metrics = FakeClock(), RecordingOutput(), RecordingOutput(), RecordingMetrics()
    local, transport = RecordingPlayer(), Transport()
    remote = RemotePlayer(transport)
    arrival, reported, speak, end = (asyncio.Event() for _ in range(4))
    reply = "One update, sir, and your answer."
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""})],
                                  [Text(reply)], [Text("One retained update, sir.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"}), Set(reported)]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    client = Client("phone", "iphone", connected=True, can_speak=True, silence=action == "silent")
    hub.attach_client(client, output=phone, player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now(), client_id="phone")
        await speak.wait()
        hub.attach_client(client)
        yield Line("What's new?", clock.now(), typed=False, client_id="phone")
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        hub.clients.disconnect("phone")
        arrival.set()
        await reported.wait()
        speak.set()
        await until(lambda: reply in phone.replies)
        assert backend.all_sessions("talker")[0].received[-1].startswith('<event source="hub" away="1">')
        if action != "silent":
            await until(lambda: len(transport.clips) == 1)
            speech_id, index, _ = transport.clips[0]
            assert remote.played() == ""
            if action == "played":
                assert remote.report(speech_id, index, "finished")
            if action == "focus":
                hub.clients.take_focus("terminal")
                assert transport.stops == []
                assert remote.report(speech_id, index, "finished")
        if action != "focus":
            hub.clients.disconnect("phone")
            if action == "disconnect":
                assert transport.stops == [transport.clips[0][0]]
                assert remote.pending() == 0
            hub.attach_client(client)
        if action == "disconnect":
            await until(lambda: len(transport.clips) == 2)
            new_id, new_index, clip = transport.clips[1]
            assert clip.text == "One retained update, sir."
            assert remote.report(new_id, new_index, "finished")
            message = backend.all_sessions("talker")[0].received[-1]
            assert message.startswith('<event source="hub" away="1">')
            assert message.count('task="T1"') == 1
        end.set()
        await running
        assert len(backend.all_sessions("talker")[0].received) == (4 if action == "disconnect" else 3)
        assert len(transport.clips) == (0 if action == "silent" else 2 if action == "disconnect" else 1)
        assert local.texts == []
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


@pytest.mark.parametrize("stopped_by_client", [False, True])
async def test_new_spoken_input_retains_news_from_interrupted_user_reply_once(stopped_by_client: bool) -> None:
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, transport = RecordingPlayer(), Transport()
    remote = RemotePlayer(transport)
    arrival, reported, speak, next_line, finish, interrupt, end = (asyncio.Event() for _ in range(7))
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""})],
                                  [Text("One update, sir. "), Gate(finish)], [Text("One update and the new answer.")]],
                          interrupt_gate=interrupt,
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"}), Set(reported)]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    client = Client("phone", "iphone", connected=True, can_speak=True)
    hub.attach_client(client, player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now(), client_id="phone")
        await speak.wait()
        hub.attach_client(client)
        yield Line("What's new?", clock.now(), typed=False, client_id="phone")
        await next_line.wait()
        yield Line("And this new question", clock.now(), typed=False, client_id="phone")
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        hub.clients.disconnect("phone")
        arrival.set()
        await reported.wait()
        speak.set()
        await until(lambda: len(transport.clips) == 1)
        if stopped_by_client:
            speech_id, index, _ = transport.clips[0]
            assert remote.report(speech_id, index, "stopped")
            finish.set()
            await until(lambda: "One update, sir. " in output.replies)
            clock.advance(0.1)
            await asyncio.sleep(0.01)
            interrupt.set()
            next_line.set()
        else:
            next_line.set()
            await until(lambda: len(transport.stops) == 1)
            finish.set()
            await until(lambda: "One update, sir. " in output.replies)
            await asyncio.sleep(0.01)
            interrupt.set()
        await until(lambda: "One update and the new answer." in output.replies)
        message = backend.all_sessions("talker")[0].received[-1]
        assert message.startswith('<event source="hub" away="1">')
        assert message.count('task="T1"') == 1
        assert "And this new question" in message
        assert len(backend.all_sessions("talker")[0].received) == 4
        end.set()
        await running
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


@pytest.mark.parametrize("heard", [False, True])
async def test_later_queued_user_reply_keeps_prior_news_until_delivery_or_retention(heard: bool) -> None:
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(hold=True), RecordingPlayer()
    arrival, reported, speak, next_line, end = (asyncio.Event() for _ in range(5))
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""})],
                                  [Text("One update and your answer.")], [Text("A queued ordinary answer.")],
                                  [Text("One retained update, sir.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"}), Set(reported)]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    client = Client("phone", "iphone", connected=True, can_speak=True)
    hub.attach_client(client, player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now(), client_id="phone")
        await speak.wait()
        yield Line("What's new?", clock.now(), typed=False)
        await next_line.wait()
        yield Line("Another typed question", clock.now())
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        hub.clients.disconnect("phone")
        arrival.set()
        await reported.wait()
        speak.set()
        await until(lambda: local.texts == ["One update and your answer."])
        events = list(local.events)
        if heard:
            local.finish()
        next_line.set()
        await until(lambda: local.texts == ["One update and your answer.", "A queued ordinary answer."])
        assert local.events[:len(events)] == events
        assert "stop" not in local.events[len(events):]
        hub.clients.disconnect("terminal")
        hub.attach_client(client)
        hub.clients.take_focus("phone")
        if not heard:
            await until(lambda: remote.texts == ["One retained update, sir."])
            message = backend.all_sessions("talker")[0].received[-1]
            assert message.startswith('<event source="hub" away="1">')
            assert message.count('task="T1"') == 1
        end.set()
        await running
        assert remote.texts == ([] if heard else ["One retained update, sir."])
        assert len(backend.all_sessions("talker")[0].received) == (4 if heard else 5)
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


async def test_remote_pending_playback_and_news_do_not_block_terminal_eof(tmp_path: Path) -> None:
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(), RecordingPlayer(hold=True)
    arrival, reported = asyncio.Event(), asyncio.Event()
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""}), Text("Working.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"}), Set(reported)]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    state_path = tmp_path / "state.json"
    hub = make_hub(backend, output, metrics, speech=speech, state_path=state_path)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now(), typed=False, client_id="phone")
        await until(lambda: remote.texts == ["Working."])
        arrival.set()
        await reported.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await reported.wait()
        await asyncio.sleep(0.01)
        clock.advance(60)
        await asyncio.wait_for(asyncio.shield(running), 0.3)
        assert output.replies == ["Working."]
        assert remote.texts == ["Working."]
        assert len(backend.all_sessions("talker")[0].received) == 2
    finally:
        running.cancel()
        await hub.close()
        await speech.close()

    restored_output = RecordingOutput()
    restored_backend = FakeBackend(clock, [[Text("Hello, sir.")]])
    restored = make_hub(restored_backend, restored_output, metrics, state_path=state_path)
    restored.attach_client(Client("phone", "iphone", connected=True))

    async def next_input() -> AsyncIterator[Line]:
        yield Line("Hello", clock.now(), client_id="phone")

    await restored.start()
    try:
        await restored.run(next_input())
        message = restored_backend.all_sessions("talker")[0].received[-1]
        assert message.startswith('<event source="hub" backlog="1">')
        assert message.count('task="T1"') == 1
        assert "Ready" in message
        assert restored_output.replies == ["Hello, sir."]
    finally:
        await restored.close()


class RecordingSoundPlayer(SoundPlayer):
    def __init__(self, sample_rate: int, open_stream: Callable[[int, Callable[[Samples], None]], FakeStream]) -> None:
        self.texts: list[str] = []
        super().__init__(sample_rate, open_stream=open_stream)

    def play(self, clip: Clip, *, starts_reply: bool = False) -> PlaybackReceipt:
        self.texts.append(clip.text)
        return super().play(clip, starts_reply=starts_reply)


@pytest.mark.parametrize("next_step", ["reconnect", "restart"])
@pytest.mark.parametrize("player_kind", ["recording", "sound"])
async def test_fully_played_news_stays_told_after_later_reply_resets_played_record(next_step: str, player_kind: str, tmp_path: Path) -> None:
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    streams: list[FakeStream] = []

    def open_stream(rate: int, fill: Callable[[Samples], None]) -> FakeStream:
        stream = FakeStream(fill)
        streams.append(stream)
        return stream

    local = RecordingPlayer(hold=True) if player_kind == "recording" else RecordingSoundPlayer(24_000, open_stream)
    arrival, reported, speak, next_line, end = (asyncio.Event() for _ in range(5))
    first, second = "One update and your answer.", "A queued ordinary answer."
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""})],
                                  [Text(first)], [Text(second)], [Text("Duplicate update.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"}), Set(reported)]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    state_path = tmp_path / "state.json"
    hub = make_hub(backend, output, metrics, speech=speech, state_path=state_path)
    client = Client("phone", "iphone", connected=True)
    hub.attach_client(client)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now(), client_id="phone")
        await speak.wait()
        yield Line("What's new?", clock.now(), typed=False)
        await next_line.wait()
        yield Line("Another typed question", clock.now())
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        hub.clients.disconnect("phone")
        arrival.set()
        await reported.wait()
        speak.set()
        await until(lambda: local.texts == [first])
        next_line.set()
        await until(lambda: local.texts == [first, second])
        if isinstance(local, RecordingPlayer):
            local.finish()
            clock.advance(1)
            await speech.idle()
        else:
            drained = asyncio.create_task(speech.idle())
            async with asyncio.timeout(1):
                while not drained.done():
                    streams[0].pull(24_000)
                    clock.advance(0.1)
                    await asyncio.sleep(0.001)
            await drained
            assert local.played() == second
        await asyncio.sleep(0.01)
        assert local.pending() == 0
        if next_step == "reconnect":
            hub.attach_client(client)
            await asyncio.sleep(0.01)
            assert output.replies == ["", first, second]
            assert len(backend.all_sessions("talker")[0].received) == 4
            end.set()
            await running
    finally:
        running.cancel()
        await hub.close()
        await speech.close()

    if next_step == "restart":
        restored_output = RecordingOutput()
        restored_backend = FakeBackend(clock, [[Text("Hello, sir.")]])
        restored = make_hub(restored_backend, restored_output, metrics, state_path=state_path)

        async def next_input() -> AsyncIterator[Line]:
            yield Line("Hello", clock.now())

        await restored.start()
        try:
            await restored.run(next_input())
            assert restored_backend.all_sessions("talker")[0].received[-1].startswith("<user")
            assert restored_output.replies == ["Hello, sir."]
        finally:
            await restored.close()


@pytest.mark.parametrize("fault", ["sentence", "voice_load"])
async def test_failed_news_synthesis_does_not_make_played_receipt_complete(fault: str) -> None:
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local = RecordingPlayer()
    arrival, reported, speak, next_line, end = (asyncio.Event() for _ in range(5))
    tts = FakeTts(load_error=RuntimeError("Voice unavailable") if fault == "voice_load" else None,
                  fail_on="Second sentence." if fault == "sentence" else None)
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""})],
                                  [Text("First sentence. Second sentence.")], [Text("One update and the new answer.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"}), Set(reported)]])
    speech = Speech(tts, local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    hub.attach_client(Client("phone", "iphone", connected=True))

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now(), client_id="phone")
        await speak.wait()
        yield Line("What's new?", clock.now(), typed=False)
        await next_line.wait()
        yield Line("And this new question", clock.now(), typed=False)
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        hub.clients.disconnect("phone")
        arrival.set()
        await reported.wait()
        speak.set()
        await until(lambda: "First sentence. Second sentence." in output.replies)
        await speech.idle()
        await asyncio.sleep(0.01)
        assert local.texts == (["First sentence."] if fault == "sentence" else [])
        assert len(backend.all_sessions("talker")[0].received) == 3
        tts.fail_on = None
        next_line.set()
        await until(lambda: "One update and the new answer." in output.replies)
        message = backend.all_sessions("talker")[0].received[-1]
        assert message.startswith('<event source="hub" away="1">')
        assert message.count('task="T1"') == 1
        assert len(backend.all_sessions("talker")[0].received) == 4
    finally:
        running.cancel()
        await hub.close()
        await speech.close()
