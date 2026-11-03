"""Voice mode through the Hub: scripted audio input in, the Talker's messages and what was played out."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from jarvis_hub.agents.backend import Context
from jarvis_hub.hub import Hub
from jarvis_hub.voice.state import Timing
from jarvis_hub.voice.voice_mode import VoiceMode, merge
from tests.fakes import (
    AwaitInterrupt,
    FakeBackend,
    FakeClock,
    FakeTts,
    Gate,
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
    tts: FakeTts

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

    async def make(*scripts: list[Step], **backend_options: Any) -> Rig:
        clock = FakeClock()
        backend = FakeBackend(clock, list(scripts), **backend_options)
        audio, player, output, metrics = ScriptedAudio(clock), RecordingPlayer(), RecordingOutput(), RecordingMetrics()
        tts = FakeTts()
        voice = VoiceMode(
            audio=audio, tts=tts, player=player, clock=clock, timing=TIMING, metrics=metrics, notice=output.notice
        )
        hub = make_hub(
            backend,
            output,
            metrics,
            speech=voice.speech,
            speak_typed=False,
            on_end_conversation=voice.end_conversation,
            quiet=voice.quiet,
        )
        typist = Typist(clock)
        await hub.start()
        voice.start()
        running = asyncio.create_task(hub.run(merge(typist.lines(), voice.lines(hub.interrupt))))
        rig = Rig(clock, backend, audio, player, output, metrics, typist, voice, hub, tts)
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


FULL = Context(100_000, 200_000)


def talker_kinds(rig: Rig) -> list[str]:
    return [r["kind"] for r in rig.metrics.records if r.get("role") == "talker"]


async def test_a_full_talker_restarts_only_once_jarvis_is_idle(rig_for: MakeRig) -> None:
    rig: Rig = await rig_for([Text("Half past nine, sir."), FULL])

    rig.audio.wake()
    rig.audio.say("What time is it?")
    await until(lambda: rig.state == "follow_up")
    await rig.settle()
    assert len(rig.backend.all_sessions("talker")) == 1

    await rig.advance(6)
    await until(lambda: len(rig.backend.all_sessions("talker")) == 2)

    assert rig.states()[-1] == "idle"
    assert talker_kinds(rig) == ["warm_up", "user", "restart"]
    assert rig.player.texts == ["", "Half past nine, sir."]


async def test_a_follow_up_said_before_idle_is_answered_on_the_old_talker_then_it_restarts(rig_for: MakeRig) -> None:
    rig: Rig = await rig_for([Text("Opened, sir."), FULL], [Text("Green, sir."), FULL])

    rig.audio.wake()
    rig.audio.say("Open a PR for the login bug")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(4)
    rig.audio.say("And is the CI green?")
    await until(lambda: len(rig.user_messages()) == 2 and rig.state == "follow_up")
    await rig.settle()

    [old] = rig.backend.all_sessions("talker")
    assert len([m for m in old.received if m.startswith("<user")]) == 2
    await rig.advance(6)
    await until(lambda: len(rig.backend.all_sessions("talker")) == 2)
    assert talker_kinds(rig) == ["warm_up", "user", "user", "restart"]


async def test_a_spoken_ask_for_max_effort_reaches_the_subagent_and_its_end_is_announced(rig_for: MakeRig) -> None:
    request = "Open a PR fixing typo X in pumpkit"
    brief = "Fix the typo in pumpkit's README, then open a PR."
    spawn = {
        "repo": "pumpkit",
        "title": "Fix typo X in pumpkit",
        "brief": brief,
        "model": "opus",
        "effort": "max",
        "writes": True,
        "branch_kind": "fix",
    }
    report = {"task_id": "T1", "spoken": "The typo fix is up.", "needs_user": False}
    finish = asyncio.Event()
    rig: Rig = await rig_for(
        [Text("Right away, sir."), Tool("delegate", {"request": request, "context": ""})],
        [Text("The typo fix is up, sir.")],
        orchestrator=[[Tool("spawn_subagent", spawn)], [Tool("report", report)]],
        subagents={brief: [Gate(finish), Text("Opened pumpkit#43.")]},
    )

    rig.audio.wake()
    rig.audio.say(request + ", max effort.")
    await until(lambda: rig.state == "follow_up" and len(rig.backend.all_sessions("subagent")) == 1)
    await rig.advance(6)
    assert rig.state == "idle"
    finish.set()
    await until(lambda: "The typo fix is up, sir." in rig.player.texts)

    [subagent] = rig.backend.all_sessions("subagent")
    assert subagent.spec.effort == "max"
    assert rig.states()[-3:] == ["idle", "announce", "follow_up"]


async def test_end_conversation_on_a_typed_line_leaves_the_spoken_exchange_its_follow_up_window(
    rig_for: MakeRig,
) -> None:
    typed_done = asyncio.Event()
    rig: Rig = await rig_for(
        [Gate(typed_done), Text("Very well, sir."), Tool("end_conversation")], [Text("Green, sir.")]
    )

    rig.typist.type("That's all for the logs")
    await rig.settle()
    rig.audio.wake()
    rig.audio.say("Is the CI green?")
    await until(lambda: rig.state == "thinking")
    typed_done.set()
    await until(lambda: "Green, sir." in rig.player.texts)
    await until(lambda: rig.state != "speaking")

    assert rig.state == "follow_up"


async def test_the_wake_word_does_not_stop_a_typed_reply(rig_for: MakeRig) -> None:
    typed_done = asyncio.Event()
    rig: Rig = await rig_for([Gate(typed_done), Text("Typed answer, sir.")], [Text("Spoken answer, sir.")])

    rig.typist.type("Summarise the logs")
    await rig.settle()
    rig.audio.wake()
    rig.audio.say("Is the CI green?")
    await until(lambda: rig.state == "thinking")
    rig.clock.advance(1)
    rig.audio.wake()
    await rig.settle()
    typed_done.set()
    await until(lambda: len(rig.output.replies) == 2)

    assert rig.output.replies[0] == "Typed answer, sir."


async def test_a_reply_stopped_by_the_wake_word_does_not_end_the_next_turn(rig_for: MakeRig) -> None:
    typed_done = asyncio.Event()
    rig: Rig = await rig_for(
        [Text("Once upon a time, sir.")], [Gate(typed_done), Text("Typed answer, sir.")], [Text("Green, sir.")]
    )
    rig.player.hold = True

    rig.audio.wake()
    rig.audio.say("Tell me a story")
    await until(lambda: rig.state == "speaking" and len(rig.output.replies) == 1)
    rig.typist.type("Summarise the logs")
    await rig.settle()
    rig.clock.advance(1)
    rig.audio.wake()
    rig.audio.say("Is the CI green?")
    await until(lambda: rig.state == "thinking")
    rig.player.finish()
    await rig.advance(0.1)

    # The story's playback ended with the stop; the CI question is still waiting for its reply.
    assert rig.state == "thinking"


async def test_news_that_arrives_while_the_user_speaks_waits_until_their_reply_has_played(rig_for: MakeRig) -> None:
    request = "Open a PR fixing typo X in pumpkit"
    brief = "Fix the typo in pumpkit's README, then open a PR."
    spawn = {
        "repo": "pumpkit",
        "title": "Fix typo X in pumpkit",
        "brief": brief,
        "model": "sonnet",
        "effort": "medium",
        "writes": True,
        "branch_kind": "fix",
    }
    report = {"task_id": "T1", "spoken": "The typo fix is up.", "needs_user": False}
    finish, news_streamed = asyncio.Event(), asyncio.Event()
    rig: Rig = await rig_for(
        [Text("Right away, sir."), Tool("delegate", {"request": request, "context": ""})],
        [Text("The typo fix is up, sir."), Gate(news_streamed)],
        [Text("Green, sir.")],
        orchestrator=[[Tool("spawn_subagent", spawn)], [Tool("report", report)]],
        subagents={brief: [Gate(finish), Text("Opened pumpkit#43.")]},
    )
    rig.audio.wake()
    rig.audio.say(request)
    await until(lambda: rig.state == "follow_up" and len(rig.backend.all_sessions("subagent")) == 1)
    await rig.advance(6)
    assert rig.state == "idle"

    rig.audio.wake()
    await rig.settle()
    rig.audio.speech()
    finish.set()
    await until(lambda: ("reply", "The typo fix is up, sir.") in rig.output.log)
    rig.audio.say("Is the CI green?")
    await until(lambda: rig.state == "thinking")
    news_streamed.set()
    await until(lambda: "Green, sir." in rig.player.texts)
    await until(lambda: "The typo fix is up, sir." in rig.player.texts)
    await until(lambda: rig.state == "follow_up")

    assert [t for t in rig.player.texts if t][-2:] == ["Green, sir.", "The typo fix is up, sir."]
    assert rig.states()[-6:] == ["listening", "thinking", "speaking", "follow_up", "speaking", "follow_up"]


NEWS = "The typo fix is up, sir."
_REQUEST = "Open a PR fixing typo X in pumpkit"
_BRIEF = "Fix the typo in pumpkit's README, then open a PR."
_SPAWN = {
    "repo": "pumpkit",
    "title": "Fix typo X in pumpkit",
    "brief": _BRIEF,
    "model": "sonnet",
    "effort": "medium",
    "writes": True,
    "branch_kind": "fix",
}
_DELEGATE: list[Step] = [Text("Right away, sir."), Tool("delegate", {"request": _REQUEST, "context": ""})]


async def news_rig(rig_for: MakeRig, finish: asyncio.Event, news: list[Step], *later: list[Step]) -> Rig:
    """A rig whose Subagent ends on `finish`, and whose Talker then says `news`: delegated, and idle again."""
    report = {"task_id": "T1", "spoken": "The typo fix is up.", "needs_user": False}
    rig: Rig = await rig_for(
        _DELEGATE,
        news,
        *later,
        orchestrator=[[Tool("spawn_subagent", _SPAWN)], [Tool("report", report)]],
        subagents={_BRIEF: [Gate(finish), Text("Opened pumpkit#43.")]},
    )
    rig.audio.wake()
    rig.audio.say(_REQUEST)
    await until(lambda: rig.state == "follow_up" and len(rig.backend.all_sessions("subagent")) == 1)
    await rig.advance(6)
    assert rig.state == "idle"
    return rig


def spoken(rig: Rig) -> list[str]:
    return [t for t in rig.player.texts if t]


async def test_news_streamed_while_idle_but_not_yet_heard_waits_when_the_user_wakes(rig_for: MakeRig) -> None:
    finish = asyncio.Event()
    rig = await news_rig(rig_for, finish, [Text(NEWS)], [Text("Green, sir.")])

    rig.tts.hold.clear()
    finish.set()
    await until(lambda: rig.output.replies[-1:] == [NEWS])
    await rig.settle()
    rig.audio.wake()
    await until(lambda: rig.state == "listening")
    rig.tts.hold.set()
    rig.audio.say("Is the CI green?")
    await until(lambda: NEWS in rig.player.texts)
    await until(lambda: rig.state == "follow_up")

    assert spoken(rig)[-2:] == ["Green, sir.", NEWS]
    assert rig.player.texts.count(NEWS) == 1


async def test_news_still_streaming_when_the_user_wakes_waits_until_their_reply_has_played(rig_for: MakeRig) -> None:
    finish, more = asyncio.Event(), asyncio.Event()
    rig = await news_rig(rig_for, finish, [Text(NEWS), Gate(more), Text(" Shall I merge it?")], [Text("Green, sir.")])

    finish.set()
    await until(lambda: ("reply", NEWS) in rig.output.log)
    rig.audio.wake()
    rig.audio.say("Is the CI green?")
    await until(lambda: rig.state == "thinking")
    more.set()
    await until(lambda: "Shall I merge it?" in rig.player.texts)
    await until(lambda: rig.state == "follow_up")

    assert spoken(rig)[-3:] == ["Green, sir.", NEWS, "Shall I merge it?"]


async def test_news_begun_in_the_follow_up_window_waits_when_the_user_starts_speaking(rig_for: MakeRig) -> None:
    finish, more = asyncio.Event(), asyncio.Event()
    report = {"task_id": "T1", "spoken": "The typo fix is up.", "needs_user": False}
    rig: Rig = await rig_for(
        _DELEGATE,
        [Text(NEWS), Gate(more)],
        [Text("Green, sir.")],
        orchestrator=[[Tool("spawn_subagent", _SPAWN)], [Tool("report", report)]],
        subagents={_BRIEF: [Gate(finish), Text("Opened pumpkit#43.")]},
    )
    rig.audio.wake()
    rig.audio.say(_REQUEST)
    await until(lambda: rig.state == "follow_up" and len(rig.backend.all_sessions("subagent")) == 1)

    finish.set()
    await until(lambda: ("reply", NEWS) in rig.output.log)
    rig.audio.say("Is the CI green?")
    await until(lambda: rig.state == "thinking")
    more.set()
    await until(lambda: NEWS in rig.player.texts)
    await until(lambda: rig.state == "follow_up")

    assert spoken(rig)[-2:] == ["Green, sir.", NEWS]


async def test_a_turn_the_user_moved_on_from_is_answered_on_screen_only(rig_for: MakeRig) -> None:
    finish, more = asyncio.Event(), asyncio.Event()
    rig = await news_rig(
        rig_for, finish, [Text(NEWS), Gate(more)], [Text("Main is red, sir.")], [Text("Green, sir.")]
    )

    rig.audio.wake()
    await rig.settle()
    finish.set()
    await until(lambda: ("reply", NEWS) in rig.output.log)
    rig.audio.say("Is main green?")
    await until(lambda: rig.state == "thinking")
    rig.clock.advance(1)
    rig.audio.wake()
    await until(lambda: rig.state == "listening")
    rig.audio.say("Is the CI green?")
    await until(lambda: rig.state == "thinking")
    more.set()
    await until(lambda: "Green, sir." in rig.player.texts)
    await until(lambda: rig.state == "follow_up")
    await rig.advance(1)

    assert "Main is red, sir." in rig.output.replies
    assert "Main is red, sir." not in rig.player.texts


async def test_news_that_calls_end_conversation_leaves_the_users_reply_its_follow_up_window(rig_for: MakeRig) -> None:
    finish, more = asyncio.Event(), asyncio.Event()
    rig = await news_rig(
        rig_for, finish, [Text(NEWS), Gate(more), Tool("end_conversation")], [Text("Green, sir.")]
    )

    rig.audio.wake()
    await rig.settle()
    finish.set()
    await until(lambda: ("reply", NEWS) in rig.output.log)
    rig.audio.say("Is the CI green?")
    await until(lambda: rig.state == "thinking")
    more.set()
    await until(lambda: NEWS in rig.player.texts)
    await until(lambda: rig.state == "follow_up")

    assert rig.states()[-5:] == ["thinking", "speaking", "follow_up", "speaking", "follow_up"]


async def test_news_held_for_a_wake_with_nothing_said_plays_once_jarvis_idles(rig_for: MakeRig) -> None:
    finish = asyncio.Event()
    rig = await news_rig(rig_for, finish, [Text(NEWS)])

    rig.audio.wake()
    await rig.settle()
    finish.set()
    await until(lambda: rig.output.replies[-1:] == [NEWS])
    await rig.settle()
    assert NEWS not in rig.player.texts

    await rig.advance(6)
    await until(lambda: NEWS in rig.player.texts)
    assert rig.states()[-3:] == ["idle", "announce", "follow_up"]


async def test_news_the_user_wakes_over_is_stopped_and_not_said_again(rig_for: MakeRig) -> None:
    finish = asyncio.Event()
    rig = await news_rig(rig_for, finish, [Text(NEWS)], [Text("Green, sir.")])
    rig.player.hold = True

    finish.set()
    await until(lambda: rig.state == "announce")
    rig.clock.advance(1)
    rig.audio.wake()
    await until(lambda: rig.state == "listening" and rig.player.events[-1] == "play ")
    rig.player.hold = False
    rig.player.finish()
    rig.audio.say("Is the CI green?")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(1)

    assert "stop" in rig.player.events
    assert spoken(rig)[-2:] == [NEWS, "Green, sir."]
