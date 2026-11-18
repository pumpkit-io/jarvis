from collections.abc import AsyncIterator

import pytest

from jarvis_hub.clients import Client
from jarvis_hub.hub import Line
from jarvis_hub.voice.speech import Speech
from tests.fakes import FakeBackend, FakeClock, FakeTts, RecordingMetrics, RecordingOutput, RecordingPlayer, Text, make_hub


async def test_reply_audio_stays_on_requesting_client_while_text_reaches_both() -> None:
    clock, tts, metrics = FakeClock(), FakeTts(), RecordingMetrics()
    desk, phone = RecordingOutput(), RecordingOutput()
    local, remote = RecordingPlayer(), RecordingPlayer()
    speech = Speech(tts, local, clock, metrics=metrics, notice=desk.notice)
    hub = make_hub(FakeBackend(clock, [[Text("On your phone, sir.")]]), desk, metrics, speech=speech)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), output=phone, player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Hello", clock.now(), typed=False, client_id="phone")

    speech.start()
    await hub.start()
    try:
        await hub.run(inputs())
        await speech.idle()
        assert local.texts == []
        assert remote.texts == ["On your phone, sir."]
        assert desk.replies == phone.replies == ["On your phone, sir."]
    finally:
        await hub.close()
        await speech.close()


async def test_remote_played_text_comes_only_from_client_reports() -> None:
    from jarvis_hub.voice.player import Clip, RemotePlayer

    class Transport:
        def __init__(self) -> None:
            self.clips: list[tuple[str, int, Clip]] = []
            self.stops: list[str] = []

        def send_clip(self, speech_id: str, index: int, clip: Clip) -> None:
            self.clips.append((speech_id, index, clip))

        def stop(self, speech_id: str) -> None:
            self.stops.append(speech_id)

    clock, tts, metrics = FakeClock(), FakeTts(), RecordingMetrics()
    output, local, transport = RecordingOutput(), RecordingPlayer(), Transport()
    remote = RemotePlayer(transport)
    speech = Speech(tts, local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(FakeBackend(clock, [[Text("First sentence. Second sentence.")]]), output, metrics, speech=speech)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Hello", clock.now(), typed=False, client_id="phone")

    from tests.fakes import until
    speech.start()
    await hub.start()
    try:
        await hub.run(inputs())
        await until(lambda: len(transport.clips) == 2)
        speech_id = transport.clips[0][0]
        assert [(index, clip.text) for _, index, clip in transport.clips] == [(0, "First sentence."), (1, "Second sentence.")]
        assert remote.played() == ""
        assert not remote.report("another-stream", 0, "finished")
        assert not remote.report(speech_id, 8, "finished")
        assert remote.report(speech_id, 0, "started")
        assert remote.played() == ""
        assert remote.report(speech_id, 0, "finished")
        assert remote.played() == "First sentence."
        await hub.interrupt()
        assert transport.stops == [speech_id]
        assert remote.pending() == 0
        assert not remote.report(speech_id, 1, "finished")
        assert remote.played() == "First sentence."
    finally:
        await hub.close()
        await speech.close()


async def test_typed_phone_reply_is_text_only_even_when_terminal_speaks_typed_lines() -> None:
    clock, tts, metrics = FakeClock(), FakeTts(), RecordingMetrics()
    output, local, remote = RecordingOutput(), RecordingPlayer(), RecordingPlayer()
    speech = Speech(tts, local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(FakeBackend(clock, [[Text("Text only.")]]), output, metrics, speech=speech)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Hello", clock.now(), client_id="phone")

    speech.start()
    await hub.start()
    try:
        await hub.run(inputs())
        await speech.idle()
        assert remote.texts == local.texts == []
        assert output.replies == ["Text only."]
    finally:
        await hub.close()
        await speech.close()


async def test_terminal_input_stops_speech_playing_on_phone() -> None:
    from tests.fakes import until

    clock, tts, metrics = FakeClock(), FakeTts(), RecordingMetrics()
    output, local, remote = RecordingOutput(), RecordingPlayer(), RecordingPlayer(hold=True)
    speech = Speech(tts, local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(FakeBackend(clock, [[Text("On the phone.")], [Text("At the desk.")]]), output, metrics, speech=speech)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Phone", clock.now(), typed=False, client_id="phone")
        await until(lambda: remote.texts == ["On the phone."])
        yield Line("Desk", clock.now())

    speech.start()
    await hub.start()
    try:
        await hub.run(inputs())
        assert remote.pending() == 0
        assert "stop" in remote.events
        assert output.replies == ["On the phone.", "At the desk."]
    finally:
        await hub.close()
        await speech.close()


@pytest.mark.parametrize("destination", ["missing_player", "disconnected", "silent", "no_speaker"])
async def test_unavailable_or_silent_destination_does_not_synthesize(destination: str) -> None:
    clock, tts, metrics = FakeClock(), FakeTts(), RecordingMetrics()
    output, local, remote = RecordingOutput(), RecordingPlayer(), RecordingPlayer()
    speech = Speech(tts, local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(FakeBackend(clock, [[Text("Still readable.")]]), output, metrics, speech=speech)
    hub.attach_client(Client("phone", "iphone", connected=destination != "disconnected",
                             can_speak=destination != "no_speaker", silence=destination == "silent"),
                      player=None if destination == "missing_player" else remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Hello", clock.now(), typed=False, client_id="phone")

    speech.start()
    await speech.idle()
    warmed = list(tts.calls)
    await hub.start()
    try:
        await hub.run(inputs())
        await speech.idle()
        assert tts.calls == warmed
        assert local.texts == remote.texts == []
        assert output.replies == ["Still readable."]
    finally:
        await hub.close()
        await speech.close()


async def test_phone_input_stops_terminal_speech_and_focus_does_not_redirect_queued_audio() -> None:
    from tests.fakes import until

    clock, tts, metrics = FakeClock(), FakeTts(), RecordingMetrics()
    output, local, remote = RecordingOutput(), RecordingPlayer(hold=True), RecordingPlayer()
    speech = Speech(tts, local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(FakeBackend(clock, [[Text("On the desk.")], [Text("On your phone.")]]), output, metrics, speech=speech)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Desk", clock.now())
        await until(lambda: local.texts == ["On the desk."])
        yield Line("Phone", clock.now(), typed=False, client_id="phone")
        hub.clients.take_focus("terminal")

    speech.start()
    await hub.start()
    try:
        await hub.run(inputs())
        await speech.idle()
        assert local.pending() == 0
        assert "stop" in local.events
        assert local.texts == ["On the desk."]
        assert remote.texts == ["On your phone."]
        assert hub.clients.focus == "terminal"
    finally:
        await hub.close()
        await speech.close()


async def test_disconnecting_during_reply_prevents_further_synthesis_without_losing_text() -> None:
    import asyncio
    from tests.fakes import Gate, until

    clock, tts, metrics = FakeClock(), FakeTts(), RecordingMetrics()
    output, local, remote = RecordingOutput(), RecordingPlayer(), RecordingPlayer()
    release = asyncio.Event()
    speech = Speech(tts, local, clock, metrics=metrics, notice=output.notice)
    backend = FakeBackend(clock, [[Text("First sentence. "), Gate(release), Text("Unread sentence.")]])
    hub = make_hub(backend, output, metrics, speech=speech)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Phone", clock.now(), typed=False, client_id="phone")
        await until(lambda: remote.texts == ["First sentence."])
        hub.clients.disconnect("phone")
        release.set()

    speech.start()
    await hub.start()
    try:
        await hub.run(inputs())
        await speech.idle()
        assert remote.texts == ["First sentence."]
        assert local.texts == []
        assert output.replies == ["First sentence. Unread sentence."]
        assert not any(text == "Unread sentence." for text, _ in tts.calls)
    finally:
        await hub.close()
        await speech.close()


async def test_phone_reply_after_interrupting_mac_does_not_leave_mac_in_spoken_turn() -> None:
    import asyncio
    from jarvis_hub.voice.state import Timing
    from jarvis_hub.voice.voice_mode import VoiceMode, merge
    from tests.fakes import ScriptedAudio, until

    clock, tts, metrics = FakeClock(), FakeTts(), RecordingMetrics()
    output, local, remote, audio = RecordingOutput(), RecordingPlayer(hold=True), RecordingPlayer(), ScriptedAudio(clock)
    voice = VoiceMode(
        audio=audio, tts=tts, player=local, clock=clock, metrics=metrics, notice=output.notice,
        timing=Timing(follow_up_s=6, announce_follow_up_s=4, listen_s=6, chime_s=0.5, tail_s=0),
    )
    backend = FakeBackend(clock, [[Text("At the desk.")], [Text("On the phone.")], [Text("Back at the desk.")]])
    hub = make_hub(backend, output, metrics, speech=voice.speech, floor=voice, speak_typed=False)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)

    end = asyncio.Event()

    async def inputs() -> AsyncIterator[Line]:
        await until(lambda: "At the desk." in local.texts)
        yield Line("Phone", clock.now(), typed=False, client_id="phone")
        await end.wait()

    await hub.start()
    voice.start()
    running = asyncio.create_task(hub.run(merge(inputs(), voice.lines(hub.interrupt, hub.woke, hub.admit_spoken_revision))))
    try:
        await until(lambda: local.voices == ["ready"])
        local.finish()
        audio.wake()
        await until(lambda: audio.capturing[-1:] == [True])
        audio.say("Desk")
        await until(lambda: remote.texts == ["On the phone."])
        await voice.idle()
        await until(lambda: audio.capturing[-1:] == [False])
        assert local.pending() == 0
        audio.say("Nearby conversation")
        audio.wake()
        await until(lambda: audio.capturing[-1:] == [True])
        audio.say("Back to work")
        await until(lambda: "Back at the desk." in local.texts)
        assert [text for kind, text in output.log if kind == "said"] == ["Desk", "Phone", "Back to work"]
        assert remote.texts == ["On the phone."]
        end.set()
        await running
    finally:
        running.cancel()
        await hub.close()
        await voice.close()


async def test_disconnected_phone_preserves_terminal_typed_reply_queue() -> None:
    from tests.fakes import until

    clock, tts, metrics = FakeClock(), FakeTts(), RecordingMetrics()
    output, local, remote = RecordingOutput(), RecordingPlayer(hold=True), RecordingPlayer()
    speech = Speech(tts, local, clock, metrics=metrics, notice=output.notice)
    hub = make_hub(FakeBackend(clock, [[Text("First reply.")], [Text("Second reply.")]]), output, metrics, speech=speech)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)
    hub.clients.disconnect("phone")

    async def inputs() -> AsyncIterator[Line]:
        yield Line("First", clock.now())
        await until(lambda: local.texts == ["First reply."])
        yield Line("Second", clock.now())

    speech.start()
    await hub.start()
    try:
        await hub.run(inputs())
        await until(lambda: local.texts == ["First reply.", "Second reply."])
        local.finish()
        assert local.played() == "First reply. Second reply."
        assert "stop" not in local.events
        assert remote.texts == []
    finally:
        await hub.close()
        await speech.close()


@pytest.mark.parametrize("phone_generating", [False, True])
async def test_mac_spoken_request_stops_phone_reply_and_keeps_its_local_floor(phone_generating: bool) -> None:
    import asyncio
    from jarvis_hub.voice.state import Timing
    from jarvis_hub.voice.voice_mode import VoiceMode, merge
    from tests.fakes import AwaitInterrupt, ScriptedAudio, Step, until

    clock, tts, metrics = FakeClock(), FakeTts(), RecordingMetrics()
    output, local, remote, audio = RecordingOutput(), RecordingPlayer(), RecordingPlayer(hold=True), ScriptedAudio(clock)
    voice = VoiceMode(
        audio=audio, tts=tts, player=local, clock=clock, metrics=metrics, notice=output.notice,
        timing=Timing(follow_up_s=6, announce_follow_up_s=4, listen_s=6, chime_s=0.5, tail_s=0),
    )
    phone_script: list[Step] = [Text("Phone reply. ")]
    if phone_generating:
        phone_script += [AwaitInterrupt(), Text("Stale phone sentence.")]
    backend = FakeBackend(clock, [phone_script, [Text("Desk reply.")], [Text("Desk follow-up.")]])
    hub = make_hub(backend, output, metrics, speech=voice.speech, floor=voice, speak_typed=False)
    hub.attach_client(Client("phone", "iphone", connected=True, can_speak=True), player=remote)

    end = asyncio.Event()

    async def inputs() -> AsyncIterator[Line]:
        await until(lambda: local.voices == ["ready"])
        yield Line("Phone", clock.now(), typed=False, client_id="phone")
        await end.wait()

    await hub.start()
    voice.start()
    running = asyncio.create_task(hub.run(merge(inputs(), voice.lines(hub.interrupt, hub.woke, hub.admit_spoken_revision))))
    try:
        await until(lambda: remote.texts == ["Phone reply."])
        assert audio.capturing[-1:] == [False]
        local.hold = True
        audio.wake()
        await until(lambda: audio.capturing[-1:] == [True])
        audio.say("Desk")
        await until(lambda: "Desk reply." in local.texts)
        assert remote.pending() == 0
        assert "stop" in remote.events
        assert remote.texts == ["Phone reply."]
        assert local.texts.count("Desk reply.") == 1
        assert not any("Stale phone sentence." in reply for reply in output.replies)
        assert hub.clients.focus == "terminal"
        await until(lambda: audio.capturing[-1:] == [False])
        local.finish()
        clock.advance(0.1)
        await until(lambda: audio.capturing[-1:] == [True])
        audio.say("And the next thing")
        await until(lambda: "Desk follow-up." in local.texts)
        local.finish()
        assert [text for kind, text in output.log if kind == "said"] == ["Phone", "Desk", "And the next thing"]
        assert remote.texts == ["Phone reply."]
        end.set()
        await running
    finally:
        running.cancel()
        await hub.close()
        await voice.close()
