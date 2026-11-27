import asyncio
from collections.abc import AsyncIterator

from jarvis_hub.clients import Client
from jarvis_hub.hub import Line
from jarvis_hub.voice.player import Clip, RemotePlayer
from jarvis_hub.voice.speech import Speech
from tests.fakes import FakeBackend, FakeClock, FakeTts, Gate, RecordingMetrics, RecordingOutput, RecordingPlayer, Set, Text, Tool, make_hub, until


class Transport:
    def __init__(self) -> None:
        self.clips: list[tuple[str, int, Clip]] = []
        self.stops: list[str] = []

    def send_clip(self, speech_id: str, index: int, clip: Clip) -> None:
        self.clips.append((speech_id, index, clip))

    def stop(self, speech_id: str) -> None:
        self.stops.append(speech_id)


async def test_prequeued_terminal_reports_batch_on_connected_focus_and_are_not_retold_after_reconnect() -> None:
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, transport = RecordingPlayer(), Transport()
    remote = RemotePlayer(transport)
    arrival, reported, release, end = (asyncio.Event() for _ in range(4))
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""}), Gate(release)],
                                  [Text("Both reports are ready.")], [Text("Two held updates, sir.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "First"}),
                                         Tool("report", {"task_id": "T1", "spoken": "Second"}), Set(reported)]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    client = Client("phone", "iphone", connected=True, can_speak=True)
    hub.attach_client(client, player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now())
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        arrival.set()
        await reported.wait()
        hub.clients.take_focus("phone")
        release.set()
        await until(lambda: len(transport.clips) >= 1)
        await asyncio.sleep(0.01)
        clock.advance(3)
        await asyncio.sleep(0.01)
        assert len(transport.clips) == 1
        speech_id, index, clip = transport.clips[0]
        assert clip.text == "Both reports are ready."
        assert remote.report(speech_id, index, "started")
        assert remote.played() == ""
        message = backend.all_sessions("talker")[0].received[-1]
        assert message.count('task="T1"') == 2
        assert "First" in message and "Second" in message
        assert len(backend.all_sessions("talker")[0].received) == 3
        hub.clients.disconnect("phone")
        assert transport.stops == [speech_id]
        hub.attach_client(client)
        await asyncio.sleep(0.01)
        # The phone's open screen showed the whole reply before the disconnect cut its speech.
        assert not remote.report(speech_id, index, "finished")
        assert len(backend.all_sessions("talker")[0].received) == 3
        assert local.texts == []
        end.set()
        await running
        assert len(transport.clips) == 1
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


async def test_spoken_input_keeps_unheard_news_while_interrupt_control_waits_for_model_end() -> None:
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, transport = RecordingPlayer(), Transport()
    remote = RemotePlayer(transport)
    arrival, finish, interrupt, speak, end = (asyncio.Event() for _ in range(5))
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""})],
                                  [Text("An unheard update. "), Gate(finish)], [Text("One update, sir, and your answer.")]],
                          interrupt_gate=interrupt,
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"})]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now(), client_id="phone")
        await speak.wait()
        yield Line("What's new?", clock.now(), typed=False)
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        arrival.set()
        await until(lambda: len(transport.clips) == 1)
        speak.set()
        await until(lambda: len(transport.stops) == 1)
        finish.set()
        await until(lambda: "An unheard update. " in output.replies)
        await asyncio.sleep(0.01)
        interrupt.set()
        await until(lambda: "One update, sir, and your answer." in output.replies)
        message = backend.all_sessions("talker")[0].received[-1]
        assert message.startswith('<event source="hub" away="1">')
        assert message.count('task="T1"') == 1
        assert "What's new?" in message
        assert len(backend.all_sessions("talker")[0].received) == 4
        assert hub.clients.focus == "terminal"
        end.set()
        await running
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


async def test_terminal_speak_batches_news_before_a_late_focus_move_preserves_both_updates() -> None:
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(hold=True), RecordingPlayer()
    arrival, reported, release, end = (asyncio.Event() for _ in range(4))
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""}), Gate(release)],
                                  [Text("Both reports are ready.")], [Text("Two held updates, sir.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "First"}),
                                         Tool("report", {"task_id": "T1", "spoken": "Second"}), Set(reported)]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now())
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        arrival.set()
        await reported.wait()
        release.set()
        await until(lambda: "Both reports are ready." in local.texts)
        await asyncio.sleep(0.01)
        clock.advance(3)
        await asyncio.sleep(0.01)
        hub.clients.take_focus("phone")
        assert local.pending() == 0
        await until(lambda: bool(remote.texts))
        message = backend.all_sessions("talker")[0].received[-1]
        assert message.startswith('<event source="hub" away="2">')
        assert message.count('task="T1"') == 2
        assert local.texts == ["Both reports are ready."]
        assert remote.texts == ["Two held updates, sir."]
        end.set()
        await running
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


async def test_terminal_speak_news_stays_audible_after_input_ends() -> None:
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local = RecordingPlayer()
    arrival, reported, release = (asyncio.Event() for _ in range(3))
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""}), Gate(release)],
                                  [Text("The fix is ready.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"}), Set(reported)]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now())
        await reported.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        arrival.set()
        await reported.wait()
        await asyncio.sleep(0.01)
        release.set()
        await running
        await speech.idle()
        assert local.texts == ["The fix is ready."]
        assert output.replies == ["", "The fix is ready."]
    finally:
        running.cancel()
        await hub.close()
        await speech.close()
