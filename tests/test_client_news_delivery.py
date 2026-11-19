import asyncio
from collections.abc import AsyncIterator

import pytest

from jarvis_hub.agents.backend import Context
from jarvis_hub.clients import Client
from jarvis_hub.hub import Line
from jarvis_hub.voice.speech import Speech
from tests.fakes import FakeBackend, FakeClock, FakeTts, Gate, RecordingMetrics, RecordingOutput, RecordingPlayer, Set, Step, Text, Tool, make_hub, until


@pytest.mark.parametrize("restart", [False, True])
async def test_queued_terminal_news_checks_focus_at_consumption_and_preserves_user_fifo(restart: bool) -> None:
    clock, output, phone, metrics = FakeClock(), RecordingOutput(), RecordingOutput(), RecordingMetrics()
    arrival, reported, release, queue_next, queued, end = (asyncio.Event() for _ in range(6))
    restart_open = asyncio.Event()
    first: list[Step] = [Tool("delegate", {"request": "Fix typo", "context": ""}), Gate(release), Text("Working, sir.")]
    if restart:
        first.append(Context(100_000, 200_000))
    backend = FakeBackend(clock, [first, [Text("Second request answered.")], [Text("Two held updates, sir.")]],
                          restart_open_gate=restart_open,
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "First"}),
                                         Tool("report", {"task_id": "T1", "spoken": "Second"}), Set(reported)]])
    hub = make_hub(backend, output, metrics)
    hub.attach_client(Client("phone", "iphone"), output=phone)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now())
        await queue_next.wait()
        yield Line("A second request", clock.now())
        queued.set()
        await end.wait()

    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        arrival.set()
        await reported.wait()
        queue_next.set()
        await queued.wait()
        if restart:
            release.set()
            await until(lambda: backend.talker_opens == 2)
        hub.clients.take_focus("phone")
        release.set()
        restart_open.set()
        await until(lambda: "Second request answered." in output.replies)
        assert output.replies == ["Working, sir.", "Second request answered."]
        assert phone.replies == []
        users = [message for session in backend.all_sessions("talker") for message in session.received if message.startswith("<user")]
        assert ["Fix typo" in users[0], "A second request" in users[1]] == [True, True]
        hub.attach_client(Client("phone", "iphone", connected=True))
        await until(lambda: "Two held updates, sir." in phone.replies)
        message = backend.all_sessions("talker")[-1].received[-1]
        assert message.startswith('<event source="hub" away="2">')
        assert message.count('task="T1"') == 2
        end.set()
        await running
        assert phone.replies == ["Two held updates, sir."]
    finally:
        running.cancel()
        await hub.close()


@pytest.mark.parametrize("silence", [False, True])
@pytest.mark.parametrize("delivery", ["unseen", "partial", "complete"])
async def test_text_only_announcement_disconnect_preserves_only_untold_news(silence: bool, delivery: str) -> None:
    clock, output, phone, metrics = FakeClock(), RecordingOutput(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(), RecordingPlayer()
    arrival, started, first, partial, last, end = (asyncio.Event() for _ in range(6))
    original = [Set(started), Gate(first), Text("First part. "), Set(partial), Gate(last), Text("Second part.")]
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""})], original, [Text("One held update, sir.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"})]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    client = Client("phone", "iphone", connected=True, can_speak=silence, silence=silence)
    hub.attach_client(client, output=phone, player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now(), client_id="phone")
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        arrival.set()
        await started.wait()
        if delivery != "unseen":
            first.set()
            await partial.wait()
        if delivery == "complete":
            last.set()
            await until(lambda: "First part. Second part." in phone.replies)
        hub.clients.disconnect("phone")
        first.set()
        last.set()
        await until(lambda: "First part. Second part." in output.replies)
        before = len(phone.log)
        hub.attach_client(client)
        if delivery != "complete":
            await until(lambda: ("reply", "One held update, sir.") in phone.log[before:])
            message = backend.all_sessions("talker")[0].received[-1]
            assert message.startswith('<event source="hub" away="1">')
            assert message.count('task="T1"') == 1
        end.set()
        await running
        new_text = [text for kind, text in phone.log[before:] if kind == "reply"]
        assert new_text == ([] if delivery == "complete" else ["One held update, sir."])
        assert local.texts == remote.texts == []
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


@pytest.mark.parametrize("generating", [False, True])
async def test_switching_visible_focus_to_silence_finishes_text_without_repeating_news(generating: bool) -> None:
    clock, output, phone, metrics = FakeClock(), RecordingOutput(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(), RecordingPlayer(hold=True)
    arrival, finish, end = (asyncio.Event() for _ in range(3))
    original: list[Step] = [Text("First part. ")]
    if generating:
        original.extend([Gate(finish), Text("Second part.")])
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""})], original],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"})]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    client = Client("phone", "iphone", connected=True, can_speak=True)
    hub.attach_client(client, output=phone, player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now(), client_id="phone")
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        arrival.set()
        await until(lambda: remote.texts == ["First part."])
        hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True, silence=True))
        assert remote.pending() == 0
        finish.set()
        expected = "First part. Second part." if generating else "First part. "
        await until(lambda: expected in phone.replies)
        end.set()
        await running
        assert phone.replies == ["", expected]
        assert len(backend.all_sessions("talker")[0].received) == 3
        assert remote.texts == ["First part."]
        assert local.texts == []
    finally:
        running.cancel()
        await hub.close()
        await speech.close()
