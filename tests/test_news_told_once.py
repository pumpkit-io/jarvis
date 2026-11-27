import asyncio
from collections.abc import AsyncIterator

import pytest

from jarvis_hub.clients import Client
from jarvis_hub.hub import Line
from jarvis_hub.voice.speech import Speech
from tests.fakes import FakeBackend, FakeClock, FakeTts, Gate, RecordingMetrics, RecordingOutput, RecordingPlayer, Text, Tool, make_hub, until

NEWS = "The pull request is ready, sir. Shall I merge it?"
REPEAT = "That's the same update again, sir."


def news_turns(backend: FakeBackend) -> list[str]:
    """The Talker turns that tell news, not the User's lines or the launch."""
    return [m for m in backend.all_sessions("talker")[0].received[1:] if m.startswith("<event")]


async def test_typed_line_over_spoken_news_does_not_retell_it() -> None:
    clock, output, phone, metrics = FakeClock(), RecordingOutput(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(), RecordingPlayer(hold=True)
    arrival, typed, end = (asyncio.Event() for _ in range(3))
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""})],
                                  [Text(NEWS)], [Text("Merging it now.")], [Text(REPEAT)]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"})]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    client = Client("phone", "iphone", connected=True, can_speak=True)
    hub.attach_client(client, output=phone, player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now(), client_id="phone")
        await typed.wait()
        yield Line("Yes", clock.now(), client_id="phone")
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        arrival.set()
        await until(lambda: NEWS in phone.replies and remote.pending() > 0)
        typed.set()
        await until(lambda: "Merging it now." in phone.replies)
        remote.finish()
        # The phone reports its playback and presence as it goes on.
        hub.attach_client(client)
        await asyncio.sleep(0.05)
        end.set()
        await running
        assert len(news_turns(backend)) == 1
        assert REPEAT not in phone.replies
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


@pytest.mark.parametrize("change", ["reconnect", "new_player"])
async def test_client_change_after_spoken_news_does_not_retell_it(change: str) -> None:
    clock, output, phone, metrics = FakeClock(), RecordingOutput(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(), RecordingPlayer(hold=True)
    arrival, second, end = (asyncio.Event() for _ in range(3))
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""})],
                                  [Text(NEWS)], [Text("It is merged, sir.")], [Text(REPEAT)]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"}),
                                         Gate(second), Tool("report", {"task_id": "T1", "spoken": "Merged"})]])
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
        await until(lambda: NEWS in phone.replies and remote.pending() > 0)
        player = remote
        if change == "reconnect":
            hub.clients.disconnect("phone")
            hub.attach_client(client)
        else:
            player = RecordingPlayer(hold=True)
            hub.attach_client(client, player=player)
        await asyncio.sleep(0.05)
        assert len(news_turns(backend)) == 1
        # Genuinely new news is still told.
        second.set()
        await until(lambda: "It is merged, sir." in phone.replies)
        assert "Merged" in news_turns(backend)[-1]
        player.finish()
        await asyncio.sleep(0.05)
        end.set()
        await running
        assert len(news_turns(backend)) == 2
        assert REPEAT not in phone.replies
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


async def test_client_change_while_news_reply_generates_does_not_retell_it() -> None:
    clock, output, phone, metrics = FakeClock(), RecordingOutput(), RecordingOutput(), RecordingMetrics()
    local, remote, replacement = RecordingPlayer(), RecordingPlayer(hold=True), RecordingPlayer(hold=True)
    arrival, finish, end = (asyncio.Event() for _ in range(3))
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""})],
                                  [Text("The pull request is ready, sir. "), Gate(finish), Text("Shall I merge it?")],
                                  [Text(REPEAT)]],
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
        await until(lambda: len(news_turns(backend)) == 1)
        hub.attach_client(client, player=replacement)
        finish.set()
        await until(lambda: NEWS in phone.replies)
        replacement.finish()
        await asyncio.sleep(0.05)
        end.set()
        await running
        assert len(news_turns(backend)) == 1
        assert REPEAT not in phone.replies
    finally:
        running.cancel()
        await hub.close()
        await speech.close()
