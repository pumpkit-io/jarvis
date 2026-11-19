import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jarvis_hub.clients import Client
from jarvis_hub.hub import Line
from jarvis_hub.voice.speech import Speech
from tests.fakes import FakeBackend, FakeClock, FakeTts, Gate, RecordingMetrics, RecordingOutput, RecordingPlayer, Text, Tool, make_hub, until


async def test_announcement_speaks_only_on_focus_client() -> None:
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(), RecordingPlayer()
    arrival, end = asyncio.Event(), asyncio.Event()
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""})], [Text("The fix is ready.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"})]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now(), client_id="phone")
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        arrival.set()
        await until(lambda: "The fix is ready." in output.replies)
        await speech.idle()
        assert remote.texts == ["The fix is ready."]
        assert local.texts == []
        end.set()
        await running
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


async def test_disconnected_focus_holds_exact_count_for_next_spoken_turn() -> None:
    from tests.fakes import Set

    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(), RecordingPlayer()
    arrival, reported, speak, end = (asyncio.Event() for _ in range(4))
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""})], [Text("Two updates, sir.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "First"}),
                                         Tool("report", {"task_id": "T1", "spoken": "Second"}), Set(reported)]])
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
        hub.clients.disconnect("phone")
        arrival.set()
        await reported.wait()
        await asyncio.sleep(0.01)
        assert output.replies == [""]
        assert remote.texts == local.texts == []
        speak.set()
        await until(lambda: "Two updates, sir." in output.replies)
        message = backend.all_sessions("talker")[0].received[-1]
        assert message.startswith('<event source="hub" away="2">2 updates while you were away')
        assert "First" in message and "Second" in message and "What's new?" in message
        assert len(output.shown) == 1
        end.set()
        await running
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


async def test_reconnecting_focus_batches_held_news_once_without_a_new_request() -> None:
    from tests.fakes import Set

    clock, output, phone, metrics = FakeClock(), RecordingOutput(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(), RecordingPlayer()
    arrival, reported, end = (asyncio.Event() for _ in range(3))
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""})], [Text("Two updates, sir.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "First"}),
                                         Tool("report", {"task_id": "T1", "spoken": "Second"}), Set(reported)]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    client = Client("phone", "iphone", connected=True, can_speak=True)
    hub.attach_client(client, player=remote, output=phone)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now(), client_id="phone")
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        hub.clients.disconnect("phone")
        arrival.set()
        await reported.wait()
        hub.attach_client(client)
        await until(lambda: "Two updates, sir." in phone.replies)
        await speech.idle()
        assert remote.texts == ["Two updates, sir."]
        assert local.texts == []
        assert len(phone.shown) == 1
        messages = backend.all_sessions("talker")[0].received
        assert messages[-1].startswith('<event source="hub" away="2">')
        hub.attach_client(client)
        end.set()
        await running
        assert remote.texts == ["Two updates, sir."]
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


async def test_phone_spoken_line_ends_quiet_mode_and_opens_with_held_count() -> None:
    from tests.fakes import Set

    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(), RecordingPlayer()
    arrival, reported, speak, end = (asyncio.Event() for _ in range(4))
    backend = FakeBackend(clock, [[Tool("quiet_mode", {"on": True}), Tool("delegate", {"request": "Fix typo", "context": ""})],
                                  [Text("One update, sir.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"}), Set(reported)]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Quiet while you work", clock.now(), client_id="phone")
        await speak.wait()
        yield Line("What's new?", clock.now(), typed=False, client_id="phone")
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        arrival.set()
        await reported.wait()
        await asyncio.sleep(0.01)
        assert output.replies == [""]
        speak.set()
        await until(lambda: "One update, sir." in output.replies)
        message = backend.all_sessions("talker")[0].received[-1]
        assert message.startswith('<event source="hub" away="1">')
        await speech.idle()
        assert remote.texts == ["One update, sir."]
        assert local.texts == []
        end.set()
        await running
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


@pytest.mark.parametrize("silence", [False, True])
async def test_news_shown_on_text_only_focus_is_told_once(silence: bool) -> None:
    clock, output, phone, metrics = FakeClock(), RecordingOutput(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(), RecordingPlayer()
    arrival, speak, end = (asyncio.Event() for _ in range(3))
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""})],
                                  [Text("The fix is ready.")], [Text("Anything else, sir?")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"})]])
    tts = FakeTts()
    speech = Speech(tts, local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=silence, silence=silence),
                      player=remote, output=phone)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now(), client_id="phone")
        await speak.wait()
        yield Line("Hello", clock.now(), typed=False, client_id="phone")
        await end.wait()

    speech.start()
    await speech.idle()
    warmed = list(tts.calls)
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        arrival.set()
        await until(lambda: "The fix is ready." in phone.replies)
        assert remote.texts == local.texts == []
        assert tts.calls == warmed
        speak.set()
        await until(lambda: "Anything else, sir?" in phone.replies)
        assert backend.all_sessions("talker")[0].received[-1].startswith("<user")
        assert remote.texts == local.texts == []
        end.set()
        await running
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


@pytest.mark.parametrize("tool", ["stand_by", "end_conversation"])
async def test_phone_conversation_tools_leave_mac_listening_callbacks_untouched(tool: str) -> None:
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(), RecordingPlayer()
    callbacks: list[str] = []
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    backend = FakeBackend(clock, [[Tool(tool, {}), Text("After the tool.")]])
    hub = make_hub(backend, output, metrics, speech=speech,
                   on_stand_by=lambda: callbacks.append("stand_by"),
                   on_end_conversation=lambda: callbacks.append("end_conversation"))
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("That's all", clock.now(), typed=False, client_id="phone")

    speech.start()
    await hub.start()
    try:
        await hub.run(inputs())
        await speech.idle()
        assert callbacks == []
        assert local.texts == []
        assert local.voices == []
        assert remote.texts == ([] if tool == "stand_by" else ["After the tool."])
    finally:
        await hub.close()
        await speech.close()


async def test_live_focus_waits_for_playback_before_batched_announcement() -> None:
    from tests.fakes import Set

    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(), RecordingPlayer(hold=True)
    arrival, reported, end = (asyncio.Event() for _ in range(3))
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""}), Text("Working, sir.")],
                                  [Text("Two updates, sir.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "First"}),
                                         Tool("report", {"task_id": "T1", "spoken": "Second"}), Set(reported)]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    client = Client("phone", "iphone", connected=True, can_speak=True)
    hub.attach_client(client, player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now(), typed=False, client_id="phone")
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: remote.texts == ["Working, sir."])
        hub.clients.take_focus("terminal")
        hub.clients.disconnect("terminal")
        arrival.set()
        await reported.wait()
        hub.clients.take_focus("phone")
        await asyncio.sleep(0.01)
        clock.advance(3)
        await asyncio.sleep(0.01)
        assert output.replies == ["Working, sir."]
        assert remote.texts == ["Working, sir."]
        remote.hold = False
        remote.finish()
        clock.advance(0.1)
        await until(lambda: remote.texts == ["Working, sir.", "Two updates, sir."])
        assert local.texts == []
        end.set()
        await running
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


async def test_taking_focus_on_phone_releases_news_without_waiting_for_mac_follow_up() -> None:
    from jarvis_hub.voice.state import Timing
    from jarvis_hub.voice.voice_mode import VoiceMode
    from tests.fakes import ScriptedAudio, Set

    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, remote, audio = RecordingPlayer(), RecordingPlayer(), ScriptedAudio(clock)
    arrival, reported = asyncio.Event(), asyncio.Event()
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""}), Text("Working, sir.")],
                                  [Text("The fix is ready.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"}), Set(reported)]])
    voice = VoiceMode(audio=audio, tts=FakeTts(), player=local, clock=clock, metrics=metrics, notice=output.notice,
                      timing=Timing(follow_up_s=6, announce_follow_up_s=4, listen_s=6, chime_s=0.5, tail_s=0))
    hub = make_hub(backend, output, metrics, speech=voice.speech, floor=voice, speak_typed=False)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)

    await hub.start()
    voice.start()
    running = asyncio.create_task(hub.run(voice.lines(hub.interrupt, hub.woke, hub.admit_spoken_revision)))
    try:
        await until(lambda: local.voices == ["ready"])
        audio.wake()
        await until(lambda: audio.capturing[-1:] == [True])
        audio.say("Fix typo")
        await until(lambda: "Working, sir." in local.texts)
        await voice.idle()
        await until(lambda: audio.capturing[-1:] == [True])
        arrival.set()
        await reported.wait()
        hub.clients.take_focus("phone")
        await until(lambda: remote.texts == ["The fix is ready."])
        assert [text for text in local.texts if text] == ["Working, sir."]
    finally:
        running.cancel()
        await hub.close()
        await voice.close()


async def test_phone_stand_by_stops_audio_already_playing() -> None:
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(), RecordingPlayer(hold=True)
    stop = asyncio.Event()
    backend = FakeBackend(clock, [[Text("An unfinished reply. "), Gate(stop), Tool("stand_by", {}), Text("Never said.")]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    callbacks: list[str] = []
    hub = make_hub(backend, output, metrics, speech=speech, on_stand_by=lambda: callbacks.append("closed"))
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("That's all", clock.now(), typed=False, client_id="phone")
        await until(lambda: remote.texts == ["An unfinished reply."])
        stop.set()

    speech.start()
    await hub.start()
    try:
        await hub.run(inputs())
        assert remote.pending() == 0
        assert "stop" in remote.events
        assert callbacks == []
        assert local.texts == []
        assert output.replies == ["An unfinished reply. "]
    finally:
        await hub.close()
        await speech.close()


async def test_needs_user_news_gets_through_quiet_mode_after_focus_reconnects() -> None:
    from tests.fakes import Set

    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(), RecordingPlayer()
    arrival, reported, speak, end = (asyncio.Event() for _ in range(4))
    backend = FakeBackend(clock, [[Tool("quiet_mode", {"on": True}), Tool("delegate", {"request": "Fix typo", "context": ""})],
                                  [Text("I need you, sir.")], [Text("One held update, sir.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Need you", "needs_user": True}),
                                         Tool("report", {"task_id": "T1", "spoken": "Need you", "needs_user": True}), Set(reported)]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    client = Client("phone", "iphone", connected=True, can_speak=True)
    hub.attach_client(client, player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Quiet while you work", clock.now(), client_id="phone")
        await speak.wait()
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
        assert remote.texts == local.texts == []
        hub.attach_client(client)
        await until(lambda: "I need you, sir." in remote.texts)
        message = backend.all_sessions("talker")[0].received[-1]
        assert 'needs_user="true"' in message
        assert 'away=' not in message
        speak.set()
        await until(lambda: "One held update, sir." in output.replies)
        assert backend.all_sessions("talker")[0].received[-1].startswith('<event source="hub" away="1">')
        end.set()
        await running
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


async def test_disconnected_focus_news_survives_input_end_as_backlog(tmp_path: Path) -> None:
    from tests.fakes import Set

    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    arrival, reported = asyncio.Event(), asyncio.Event()
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""})]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"}), Set(reported)]])
    state_path = tmp_path / "state.json"
    hub = make_hub(backend, output, metrics, state_path=state_path)
    hub.attach_client(Client("phone", "iphone", connected=True))

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now(), client_id="phone")
        await reported.wait()

    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        hub.clients.disconnect("phone")
        arrival.set()
        await asyncio.wait_for(running, 1)
        assert output.replies == [""]
    finally:
        running.cancel()
        await hub.close()

    recovered_output = RecordingOutput()
    recovered_backend = FakeBackend(clock, [[Text("Hello, sir.")]])
    recovered = make_hub(recovered_backend, recovered_output, metrics, state_path=state_path)
    recovered.attach_client(Client("phone", "iphone", connected=True))

    async def next_input() -> AsyncIterator[Line]:
        yield Line("Hello", clock.now(), typed=False, client_id="phone")

    await recovered.start()
    try:
        await recovered.run(next_input())
        message = recovered_backend.all_sessions("talker")[0].received[-1]
        assert message.startswith('<event source="hub" backlog="1">')
        assert 'away=' not in message
        assert recovered_output.replies == ["Hello, sir."]
    finally:
        await recovered.close()


@pytest.mark.parametrize("generating", [False, True])
async def test_focus_move_stops_old_announcement_and_tells_unheard_news_once(generating: bool) -> None:
    from tests.fakes import Step

    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(), RecordingPlayer(hold=True)
    arrival, finish, end = (asyncio.Event() for _ in range(3))
    original: list[Step] = [Text("The fix is ready. ")]
    if generating:
        original.extend([Gate(finish), Text("Old destination ending.")])
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""})],
                                  original, [Text("One update, sir: the fix is ready.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"})]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now(), client_id="phone")
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        arrival.set()
        await until(lambda: remote.texts == ["The fix is ready."])
        await asyncio.sleep(0.01)
        clock.advance(3)
        await asyncio.sleep(0.01)
        assert len(backend.all_sessions("talker")[0].received) == 3
        hub.clients.take_focus("terminal")
        assert remote.pending() == 0
        assert "stop" in remote.events
        finish.set()
        await until(lambda: local.texts == ["One update, sir: the fix is ready."])
        assert remote.texts == ["The fix is ready."]
        replacement = backend.all_sessions("talker")[0].received[-1]
        assert replacement.startswith('<event source="hub" away="1">')
        assert replacement.count('task="T1"') == 1
        await speech.idle()
        await asyncio.sleep(0.01)
        hub.clients.take_focus("phone")
        end.set()
        await running
        assert local.texts == ["One update, sir: the fix is ready."]
        assert remote.texts == ["The fix is ready."]
        assert output.replies[-1] == "One update, sir: the fix is ready."
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


async def test_focus_move_after_announcement_was_heard_does_not_repeat_it() -> None:
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(), RecordingPlayer(hold=True)
    arrival, end = asyncio.Event(), asyncio.Event()
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""})], [Text("The fix is ready.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"})]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(backend, output, metrics, speech=speech)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Fix typo", clock.now(), client_id="phone")
        await end.wait()

    speech.start()
    await hub.start()
    running = asyncio.create_task(hub.run(inputs()))
    try:
        await until(lambda: len(backend.all_sessions("orchestrator")) == 1)
        arrival.set()
        await until(lambda: remote.texts == ["The fix is ready."] and "The fix is ready." in output.replies)
        remote.finish()
        events = list(remote.events)
        hub.clients.take_focus("terminal")
        assert remote.events == events
        end.set()
        await running
        assert local.texts == []
        assert output.replies == ["", "The fix is ready."]
    finally:
        running.cancel()
        await hub.close()
        await speech.close()


async def test_focus_move_during_mac_announcement_leaves_mac_available_for_next_wake() -> None:
    from jarvis_hub.voice.state import Timing
    from jarvis_hub.voice.voice_mode import VoiceMode
    from tests.fakes import ScriptedAudio

    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, remote, audio = RecordingPlayer(), RecordingPlayer(), ScriptedAudio(clock)
    arrival = asyncio.Event()
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Fix typo", "context": ""}), Text("Working, sir.")],
                                  [Text("The fix is ready.")], [Text("One update, sir: ready.")], [Text("Back at the desk.")]],
                          orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Ready"})]])
    voice = VoiceMode(audio=audio, tts=FakeTts(), player=local, clock=clock, metrics=metrics, notice=output.notice,
                      timing=Timing(follow_up_s=6, announce_follow_up_s=4, listen_s=6, chime_s=0.5, tail_s=0))
    hub = make_hub(backend, output, metrics, speech=voice.speech, floor=voice, speak_typed=False)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)
    await hub.start()
    voice.start()
    running = asyncio.create_task(hub.run(voice.lines(hub.interrupt, hub.woke, hub.admit_spoken_revision)))
    try:
        await until(lambda: local.voices == ["ready"])
        audio.wake()
        await until(lambda: audio.capturing[-1:] == [True])
        audio.say("Fix typo")
        await until(lambda: "Working, sir." in local.texts)
        await voice.idle()
        await until(lambda: audio.capturing[-1:] == [True])
        clock.advance(6.1)
        await until(lambda: "closing" in local.voices)
        local.hold = True
        arrival.set()
        await until(lambda: "The fix is ready." in local.texts)
        closing = local.voices.count("closing")
        hub.clients.take_focus("phone")
        assert local.pending() == 0
        await until(lambda: remote.texts == ["One update, sir: ready."])
        assert local.voices.count("closing") == closing
        local.hold = False
        audio.wake()
        await until(lambda: audio.capturing[-1:] == [True])
        audio.say("At the desk again")
        await until(lambda: "Back at the desk." in local.texts)
        assert remote.texts == ["One update, sir: ready."]
    finally:
        running.cancel()
        await hub.close()
        await voice.close()
