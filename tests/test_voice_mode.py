"""Voice mode through the Hub: scripted audio input in, the Talker's messages and what was played out."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import pytest

from jarvis_hub.hub import Hub
from jarvis_hub.voice.state import Timing
from jarvis_hub.voice.voice_mode import VoiceMode, merge
from tests.fakes import (
    AwaitInterrupt,
    FakeBackend,
    FakeClock,
    FakeTts,
    RecordingMetrics,
    RecordingOutput,
    RecordingPlayer,
    ScriptedAudio,
    Step,
    Text,
    Tool,
    Typist,
    make_hub,
    until,
)

TIMING = Timing(follow_up_s=6.0, announce_follow_up_s=4.0, listen_s=6.0, chime_s=0.5)


@dataclass
class Rig:
    clock: FakeClock
    backend: FakeBackend
    audio: ScriptedAudio
    player: RecordingPlayer
    output: RecordingOutput
    metrics: RecordingMetrics
    typist: Typist
    voice: VoiceMode
    hub: Hub

    def user_messages(self) -> list[str]:
        return [m for s in self.backend.all_sessions("talker") for m in s.received if m.startswith("<user")]

    def states(self) -> list[str]:
        return [r["to"] for r in self.metrics.records if r["kind"] == "voice"]

    @property
    def state(self) -> str:
        return self.voice.state

    async def settle(self) -> None:
        """Lets every task run until nothing moves without the clock."""
        for _ in range(50):
            await asyncio.sleep(0)

    async def advance(self, seconds: float) -> None:
        await self.settle()
        self.clock.advance(seconds)
        await self.settle()


MakeRig = Callable[..., Awaitable[Rig]]


@pytest.fixture
async def rig_for() -> AsyncIterator[MakeRig]:
    rigs: list[tuple[Rig, asyncio.Task[None]]] = []

    async def make(*scripts: list[Step]) -> Rig:
        clock = FakeClock()
        backend = FakeBackend(clock, list(scripts))
        audio, player, output, metrics = ScriptedAudio(clock), RecordingPlayer(), RecordingOutput(), RecordingMetrics()
        voice = VoiceMode(
            audio=audio, tts=FakeTts(), player=player, clock=clock, timing=TIMING, metrics=metrics, notice=output.notice
        )
        hub = make_hub(
            backend, output, metrics, speech=voice.speech, speak_typed=False, on_end_conversation=voice.end_conversation
        )
        typist = Typist(clock)
        await hub.start()
        voice.start()
        running = asyncio.create_task(hub.run(merge(typist.lines(), voice.lines(hub.interrupt))))
        rig = Rig(clock, backend, audio, player, output, metrics, typist, voice, hub)
        rigs.append((rig, running))
        await until(lambda: player.texts == ["At your service, sir."])
        player.clips.clear()
        player.events.clear()
        return rig

    yield make
    for rig, running in rigs:
        rig.typist.end()
        await asyncio.wait([running], timeout=1)
        await rig.hub.close()
        await rig.voice.close()


async def test_wake_ask_hear_the_answer_then_idle_silently(rig_for: MakeRig) -> None:
    rig: Rig = await rig_for([Text("It is half past nine, sir.")])

    rig.audio.wake()
    await rig.settle()
    rig.audio.say("What time is it?")
    await until(lambda: rig.state == "follow_up")

    assert rig.user_messages() == ['<user lang="en">What time is it?</user>']
    assert rig.player.texts == ["", "It is half past nine, sir."]
    assert rig.player.voices[0] == "chime"
    assert rig.output.log[:1] == [("said", "What time is it?")]

    await rig.advance(5.9)
    assert rig.state == "follow_up"
    await rig.advance(0.1)
    assert rig.state == "idle"
    await rig.advance(60)
    assert rig.player.texts == ["", "It is half past nine, sir."]
    assert rig.states() == ["listening", "thinking", "speaking", "follow_up", "idle"]
    assert len(rig.user_messages()) == 1


async def test_hey_jarvis_and_the_request_in_one_breath(rig_for: MakeRig) -> None:
    rig: Rig = await rig_for([Text("Right away, sir.")])

    rig.audio.speech()
    rig.audio.wake()
    rig.audio.say("Hey Jarvis, open a PR for the login bug.")
    await until(lambda: rig.state == "follow_up")

    assert rig.user_messages() == ['<user lang="en">open a PR for the login bug.</user>']


async def test_a_follow_up_inside_the_window_needs_no_wake_word(rig_for: MakeRig) -> None:
    rig: Rig = await rig_for([Text("Opened, sir.")], [Text("Green, sir.")])

    rig.audio.wake()
    rig.audio.say("Open a PR for the login bug")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(4)
    rig.audio.say("And is the CI green?")
    await until(lambda: len(rig.user_messages()) == 2 and rig.state == "follow_up")

    assert rig.user_messages()[-1] == '<user lang="en">And is the CI green?</user>'
    assert [t for t in rig.player.texts if t] == ["Opened, sir.", "Green, sir."]
    assert rig.player.voices.count("chime") == 1


async def test_nothing_said_while_idle_reaches_the_model(rig_for: MakeRig) -> None:
    rig: Rig = await rig_for()

    rig.audio.say("Open a PR for the login bug")
    await rig.advance(30)
    rig.audio.say("Merge it")
    await rig.settle()

    assert rig.user_messages() == []
    assert rig.player.texts == []
    assert rig.state == "idle"


async def test_after_the_wake_word_silence_idles_without_a_word(rig_for: MakeRig) -> None:
    rig: Rig = await rig_for()

    rig.audio.wake()
    await rig.advance(6)

    assert rig.state == "idle"
    assert rig.user_messages() == []
    assert rig.player.voices == ["chime"]


async def test_a_noise_only_turn_is_dropped(rig_for: MakeRig) -> None:
    rig: Rig = await rig_for()

    rig.audio.wake()
    rig.audio.say("Hmm.")
    rig.audio.say("")
    await rig.settle()

    assert rig.user_messages() == []
    assert rig.state == "listening"


async def test_a_stop_phrase_says_very_good_and_idles_without_the_model(rig_for: MakeRig) -> None:
    rig: Rig = await rig_for([Text("Done, signore.")])

    rig.audio.wake()
    rig.audio.say("Apri una issue")
    await until(lambda: rig.state == "follow_up")
    rig.audio.say("Grazie Jarvis.")
    await until(lambda: rig.states()[-1:] == ["idle"])
    await rig.settle()

    assert len(rig.user_messages()) == 1
    assert rig.player.texts[-1] == "Molto bene, signore."


async def test_spoken_italian_is_tagged_and_answered_in_italian(rig_for: MakeRig) -> None:
    rig: Rig = await rig_for([Text("Fatto, signore.")])

    rig.audio.wake()
    rig.audio.say("Apri una issue su pumpkit per il bug del login")
    await until(lambda: rig.state == "follow_up")

    assert rig.user_messages() == ['<user lang="it">Apri una issue su pumpkit per il bug del login</user>']
    assert rig.player.voices[-1] == "im_nicola"


async def test_a_typed_line_gets_a_text_reply_and_leaves_the_voice_idle(rig_for: MakeRig) -> None:
    rig: Rig = await rig_for([Text("All quiet, sir.")])

    rig.typist.type("Anything new?")
    await until(lambda: rig.output.replies == ["All quiet, sir."])
    await rig.advance(10)

    assert rig.user_messages() == ['<user lang="en" typed="true">Anything new?</user>']
    assert rig.player.texts == []
    assert rig.state == "idle"
    assert rig.states() == []


async def test_a_typed_line_during_the_follow_up_window_is_not_spoken(rig_for: MakeRig) -> None:
    rig: Rig = await rig_for([Text("Hello, sir.")], [Text("Quietly, sir.")])

    rig.audio.wake()
    rig.audio.say("Hello")
    await until(lambda: rig.state == "follow_up")
    rig.typist.type("Say it quietly")
    await until(lambda: len(rig.output.replies) == 2)
    await rig.advance(6)

    assert [t for t in rig.player.texts if t] == ["Hello, sir."]
    assert rig.state == "idle"


async def test_the_wake_word_while_jarvis_speaks_stops_it_and_listens(rig_for: MakeRig) -> None:
    rig: Rig = await rig_for([Text("This is a very long answer, sir."), AwaitInterrupt()])

    rig.audio.wake()
    rig.audio.say("Tell me a story")
    await until(lambda: rig.state == "speaking")
    rig.clock.advance(1)
    rig.audio.wake()
    await until(lambda: rig.state == "listening" and rig.player.events[-1] == "play ")

    assert "stop" in rig.player.events
    assert rig.player.voices[-1] == "chime"
    talker = rig.backend.session("talker")
    await until(lambda: not talker._sending)  # pyright: ignore[reportPrivateUsage]


async def test_end_conversation_idles_after_the_reply_with_no_follow_up_window(rig_for: MakeRig) -> None:
    rig: Rig = await rig_for([Text("Very well, sir."), Tool("end_conversation")])

    rig.audio.wake()
    rig.audio.say("We're done for today")
    await until(lambda: rig.states()[-1:] == ["idle"])

    assert rig.states() == ["listening", "thinking", "speaking", "idle"]


async def test_capture_is_on_only_while_listening_for_a_turn(rig_for: MakeRig) -> None:
    rig: Rig = await rig_for([Text("Hi, sir.")])

    rig.audio.wake()
    rig.audio.say("Hello")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(6)

    assert rig.audio.capturing == [True, False, True, False]
