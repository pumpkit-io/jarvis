import asyncio
from collections.abc import AsyncIterator

from jarvis_hub.clients import Client
from jarvis_hub.hub import Line
from jarvis_hub.voice.speech import Speech
from tests.fakes import FakeBackend, FakeClock, FakeTts, Gate, RecordingMetrics, RecordingOutput, RecordingPlayer, Text, Tool, make_hub, until


async def test_departed_ordinary_phone_reply_does_not_block_available_focus_news() -> None:
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(), RecordingPlayer(hold=True)
    arrival, end = asyncio.Event(), asyncio.Event()
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""}), Text("Working.")],
                                  [Text("Ready now.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"})]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now(), typed=False, client_id="phone")
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: remote.texts == ["Working."])
        events = list(remote.events)
        hub.clients.take_focus("terminal")
        assert remote.pending() > 0
        assert remote.events == events
        hub.clients.disconnect("phone")
        arrival.set()
        clock.advance(1000)
        await until(lambda: local.texts == ["Ready now."])
        assert remote.pending() == 0
        assert "stop" in remote.events[len(events):]
        assert remote.texts == ["Working."]
        assert output.replies == ["Working.", "Ready now."]
        end.set()
        await running
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


async def test_old_client_disconnect_does_not_stop_newer_clients_reply() -> None:
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, old, newer = RecordingPlayer(), RecordingPlayer(hold=True), RecordingPlayer(hold=True)
    ask, finish, end = (asyncio.Event() for _ in range(3))
    backend = FakeBackend(clock, [[Text("Old reply.")], [Text("New reply. "), Gate(finish), Text("New ending.")]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    hub.attach_client(Client("old", "iphone", connected=True, can_speak=True), player=old)
    hub.attach_client(Client("new", "iphone", connected=True, can_speak=True), player=newer)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("First", clock.now(), typed=False, client_id="old")
        await ask.wait()
        yield Line("Second", clock.now(), typed=False, client_id="new")
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: old.texts == ["Old reply."])
        ask.set()
        await until(lambda: newer.texts == ["New reply."])
        events = list(newer.events)
        hub.clients.disconnect("old")
        assert newer.events == events
        assert newer.pending() > 0
        finish.set()
        await until(lambda: newer.texts == ["New reply.", "New ending."])
        assert "stop" not in newer.events[len(events):]
        newer.finish()
        end.set()
        await running
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


async def test_replaced_player_retires_old_audio_without_redirecting_the_reply() -> None:
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, old, newer = RecordingPlayer(), RecordingPlayer(hold=True), RecordingPlayer()
    finish, ask, end = (asyncio.Event() for _ in range(3))
    backend = FakeBackend(clock, [[Text("Old sentence. "), Gate(finish), Text("Text after replacement.")],
                                  [Text("New reply.")]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    client = Client("phone", "iphone", connected=True, can_speak=True)
    hub.attach_client(client, player=old)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("First", clock.now(), typed=False, client_id="phone")
        await ask.wait()
        yield Line("Second", clock.now(), typed=False, client_id="phone")
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: old.texts == ["Old sentence."])
        hub.attach_client(client, player=newer)
        assert old.pending() == 0
        finish.set()
        await until(lambda: "Old sentence. Text after replacement." in output.replies)
        await speech.idle()
        assert old.texts == ["Old sentence."]
        assert newer.texts == []
        ask.set()
        await until(lambda: newer.texts == ["New reply."])
        end.set()
        await running
        assert local.texts == []
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


async def test_typed_user_reply_survives_focus_move_after_interrupting_announcement_on_same_player() -> None:
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(hold=True), RecordingPlayer()
    arrival, ask, end = (asyncio.Event() for _ in range(3))
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""})],
                                  [Text("Task ready.")], [Text("Ordinary answer.")], [Text("One retained news report.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"})]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    client = Client("phone", "iphone", connected=False, can_speak=True)
    hub.attach_client(client, player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now())
        await ask.wait()
        yield Line("An ordinary typed question", clock.now())
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        arrival.set()
        await until(lambda: local.texts == ["Task ready."])
        ask.set()
        await until(lambda: local.texts == ["Task ready.", "Ordinary answer."])
        events = list(local.events)
        hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True))
        hub.clients.take_focus("phone")
        assert local.pending() > 0
        assert "stop" not in local.events[len(events):]
        assert remote.texts == []
        local.finish()
        clock.advance(0.1)
        await until(lambda: remote.texts == ["One retained news report."])
        message = backend.all_sessions("talker")[0].received[-1]
        assert message.startswith('<event source="hub" away="1">')
        assert message.count('task="T1"') == 1
        assert len(backend.all_sessions("talker")[0].received) == 5
        end.set()
        await running
    finally:
        running.cancel()
        await hub.close()
        await speech.close()
