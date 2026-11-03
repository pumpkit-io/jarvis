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
    # `Hub.run` on the typed and spoken lines.
    running: asyncio.Task[None]

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
            floor=voice,
        )
        typist = Typist(clock)
        await hub.start()
        voice.start()
        running = asyncio.create_task(hub.run(merge(typist.lines(), voice.lines(hub.interrupt))))
        rig = Rig(clock, backend, audio, player, output, metrics, typist, voice, hub, tts, running)
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


NEWS = "The typo fix is up, sir."
DOCS = "The docs are merged, sir."
_REPORTS = ["The typo fix is up.", "The docs are merged."]


async def news_rig(rig_for: MakeRig, *scripts: list[Step], items: int = 1) -> tuple[Rig, list[asyncio.Event]]:
    """A rig that delegated `items` Tasks and is idle again; setting `arrivals[i]` brings Task i's news to the Hub.

    `scripts` are the Talker's turns after the delegation, in the order the Talker takes them.
    """
    arrivals = [asyncio.Event() for _ in range(items)]
    delegating: list[Step] = [Text("Right away, sir.")]
    delegating += [Tool("delegate", {"request": f"Task {i + 1}", "context": ""}) for i in range(items)]
    reports = [
        [Gate(arrival), Tool("report", {"task_id": f"T{i + 1}", "spoken": _REPORTS[i], "needs_user": False})]
        for i, arrival in enumerate(arrivals)
    ]
    rig: Rig = await rig_for(delegating, *scripts, orchestrator=reports)
    rig.audio.wake()
    rig.audio.say("Fix the typo and merge the docs")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(6)
    assert rig.state == "idle"
    return rig, arrivals


def spoken(rig: Rig) -> list[str]:
    return [t for t in rig.player.texts if t]


def reports(rig: Rig) -> int:
    return sum(r.name == "report" for s in rig.backend.all_sessions("orchestrator") for r in s.tool_results)


async def arrive(rig: Rig, arrival: asyncio.Event) -> None:
    """Lets the Orchestrator report, and waits until the Hub has the news."""
    before = reports(rig)
    arrival.set()
    await until(lambda: reports(rig) > before)
    await rig.settle()


@pytest.mark.parametrize("moment", ["listening", "thinking", "speaking"])
async def test_news_that_arrives_while_the_user_has_the_floor_reaches_the_talker_once_jarvis_is_quiet(
    rig_for: MakeRig, moment: str
) -> None:
    answer = asyncio.Event()
    rig, [arrival] = await news_rig(rig_for, [Gate(answer), Text("Green, sir.")], [Text(NEWS)])

    rig.audio.wake()
    await until(lambda: rig.state == "listening")
    if moment == "listening":
        await arrive(rig, arrival)
    rig.audio.say("Is the CI green?")
    await until(lambda: rig.state == "thinking")
    if moment == "thinking":
        await arrive(rig, arrival)
    rig.player.hold = moment == "speaking"
    answer.set()
    if moment == "speaking":
        await until(lambda: rig.state == "speaking")
        await arrive(rig, arrival)
        rig.player.hold = False
        rig.player.finish()
        await rig.advance(0.1)
    await until(lambda: rig.state == "follow_up")
    await rig.settle()

    assert talker_kinds(rig)[-1] == "user"
    assert NEWS not in rig.output.replies
    await rig.advance(6)
    await until(lambda: NEWS in rig.player.texts and rig.state == "follow_up")

    assert talker_kinds(rig)[-2:] == ["user", "event"]
    assert spoken(rig)[-2:] == ["Green, sir.", NEWS]
    assert rig.states()[-3:] == ["idle", "announce", "follow_up"]


async def test_news_that_arrives_in_the_follow_up_window_waits_for_idle(rig_for: MakeRig) -> None:
    rig, [arrival] = await news_rig(rig_for, [Text("Green, sir.")], [Text(NEWS)])

    rig.audio.wake()
    rig.audio.say("Is the CI green?")
    await until(lambda: rig.state == "follow_up")
    await arrive(rig, arrival)
    await rig.advance(5)
    assert NEWS not in rig.output.replies

    await rig.advance(1)
    await until(lambda: NEWS in rig.player.texts)
    assert rig.states()[-3:] == ["idle", "announce", "follow_up"]


async def test_several_news_items_wait_in_order_and_each_plays_once(rig_for: MakeRig) -> None:
    rig, arrivals = await news_rig(rig_for, [Text("Green, sir.")], [Text(NEWS)], [Text(DOCS)], items=2)

    rig.audio.wake()
    await until(lambda: rig.state == "listening")
    for arrival in arrivals:
        await arrive(rig, arrival)
    rig.audio.say("Is the CI green?")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(6)
    await until(lambda: NEWS in rig.player.texts and rig.state == "follow_up")
    await rig.settle()
    assert DOCS not in rig.output.replies
    await rig.advance(4)
    await until(lambda: DOCS in rig.player.texts and rig.state == "follow_up")

    assert talker_kinds(rig)[-3:] == ["user", "event", "event"]
    assert spoken(rig)[-3:] == ["Green, sir.", NEWS, DOCS]


async def test_a_wake_cuts_unheard_news_and_the_news_after_it_still_plays(rig_for: MakeRig) -> None:
    rig, arrivals = await news_rig(rig_for, [Text(NEWS)], [Text("Green, sir.")], [Text(DOCS)], items=2)

    rig.tts.hold.clear()
    await arrive(rig, arrivals[0])
    await until(lambda: rig.output.replies[-1:] == [NEWS])
    await arrive(rig, arrivals[1])
    rig.audio.wake()
    await until(lambda: rig.state == "listening")
    rig.tts.hold.set()
    rig.audio.say("Is the CI green?")
    await until(lambda: "Green, sir." in rig.player.texts and rig.state == "follow_up")
    await rig.advance(6)
    await until(lambda: DOCS in rig.player.texts and rig.state == "follow_up")
    await rig.advance(1)

    assert talker_kinds(rig)[-3:] == ["event", "user", "event"]
    assert NEWS not in rig.player.texts
    assert spoken(rig)[-2:] == ["Green, sir.", DOCS]


async def test_a_second_wake_stops_the_users_first_turn_and_leaves_waiting_news_whole(rig_for: MakeRig) -> None:
    rig, [arrival] = await news_rig(
        rig_for,
        [Text("Main is"), AwaitInterrupt()],
        [Text("Green, sir.")],
        [Text(NEWS), Text(" Shall I merge it?")],
    )

    rig.audio.wake()
    await until(lambda: rig.state == "listening")
    await arrive(rig, arrival)
    rig.audio.say("Is main green?")
    await until(lambda: rig.state == "thinking" and rig.output.log[-1] == ("reply", "Main is"))
    rig.clock.advance(1)
    rig.audio.wake()
    await until(lambda: rig.state == "listening")
    rig.audio.say("Is the CI green?")
    await until(lambda: "Green, sir." in rig.player.texts and rig.state == "follow_up")
    await rig.advance(6)
    await until(lambda: "Shall I merge it?" in rig.player.texts and rig.state == "follow_up")

    assert talker_kinds(rig)[-3:] == ["user", "user", "event"]
    assert spoken(rig)[-3:] == ["Green, sir.", NEWS, "Shall I merge it?"]


async def test_news_the_user_wakes_over_is_stopped_and_not_said_again(rig_for: MakeRig) -> None:
    rig, [arrival] = await news_rig(rig_for, [Text(NEWS)], [Text("Green, sir.")])
    rig.player.hold = True

    await arrive(rig, arrival)
    await until(lambda: rig.state == "announce")
    rig.clock.advance(1)
    rig.audio.wake()
    await until(lambda: rig.state == "listening" and rig.player.events[-1] == "play ")
    rig.player.hold = False
    rig.player.finish()
    rig.audio.say("Is the CI green?")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(7)

    assert "stop" in rig.player.events
    assert spoken(rig)[-2:] == [NEWS, "Green, sir."]
    assert rig.player.texts.count(NEWS) == 1


async def test_news_waiting_for_a_wake_with_nothing_said_plays_once_jarvis_idles(rig_for: MakeRig) -> None:
    rig, [arrival] = await news_rig(rig_for, [Text(NEWS)])

    rig.audio.wake()
    await until(lambda: rig.state == "listening")
    await arrive(rig, arrival)
    assert NEWS not in rig.output.replies

    await rig.advance(6)
    await until(lambda: NEWS in rig.player.texts)
    assert rig.states()[-3:] == ["idle", "announce", "follow_up"]


async def test_news_that_calls_end_conversation_keeps_its_follow_up_window(rig_for: MakeRig) -> None:
    rig, [arrival] = await news_rig(rig_for, [Text(NEWS), Tool("end_conversation")])

    await arrive(rig, arrival)
    await until(lambda: NEWS in rig.player.texts and rig.state == "follow_up")

    assert rig.states()[-3:] == ["idle", "announce", "follow_up"]


async def test_a_full_talker_restarts_before_waiting_news_and_the_news_goes_to_the_new_session(
    rig_for: MakeRig,
) -> None:
    rig, [arrival] = await news_rig(rig_for, [Text("Green, sir."), FULL], [Text(NEWS)])

    rig.audio.wake()
    await until(lambda: rig.state == "listening")
    await arrive(rig, arrival)
    rig.audio.say("Is the CI green?")
    await until(lambda: rig.state == "follow_up")
    await rig.settle()
    assert len(rig.backend.all_sessions("talker")) == 1

    await rig.advance(6)
    await until(lambda: NEWS in rig.player.texts)

    assert talker_kinds(rig)[-3:] == ["user", "restart", "event"]
    _, new = rig.backend.all_sessions("talker")
    assert new.received[-1].startswith('<event source="orchestrator" task="T1"')


async def test_a_turn_the_user_moved_on_from_is_answered_on_screen_only(rig_for: MakeRig) -> None:
    typed_done = asyncio.Event()
    rig: Rig = await rig_for(
        [Gate(typed_done), Text("Typed answer, sir.")], [Text("Main is red, sir.")], [Text("Green, sir.")]
    )

    rig.typist.type("Summarise the logs")
    await rig.settle()
    rig.audio.wake()
    rig.audio.say("Is main green?")
    await until(lambda: rig.state == "thinking")
    rig.clock.advance(1)
    rig.audio.wake()
    rig.audio.say("Is the CI green?")
    await until(lambda: rig.state == "thinking" and len(rig.user_messages()) == 1)
    typed_done.set()
    await until(lambda: "Green, sir." in rig.player.texts and rig.state == "follow_up")

    assert "Main is red, sir." in rig.output.replies
    assert "Main is red, sir." not in rig.player.texts


async def test_news_after_the_input_ends_in_the_follow_up_window_is_shown_and_the_hub_stops(rig_for: MakeRig) -> None:
    rig, [arrival] = await news_rig(rig_for, [Text("Green, sir.")], [Text(NEWS)])
    rig.audio.wake()
    rig.audio.say("Is the CI green?")
    await until(lambda: rig.state == "follow_up")

    rig.typist.end()
    await rig.settle()
    await arrive(rig, arrival)
    await rig.advance(30)

    await until(lambda: rig.running.done())
    assert NEWS in rig.output.replies
    assert NEWS not in rig.player.texts


async def test_news_after_the_input_ends_while_idle_is_all_shown_and_the_hub_stops(rig_for: MakeRig) -> None:
    rig, arrivals = await news_rig(rig_for, [Text(NEWS)], [Text(DOCS)], items=2)

    rig.typist.end()
    await rig.settle()
    for arrival in arrivals:
        await arrive(rig, arrival)
    await rig.advance(30)

    await until(lambda: rig.running.done())
    assert [r for r in rig.output.replies if r in (NEWS, DOCS)] == [NEWS, DOCS]


async def test_a_wake_word_queued_as_news_claims_the_voice_interrupts_the_news(rig_for: MakeRig) -> None:
    rig, [arrival] = await news_rig(rig_for, [Text(NEWS), AwaitInterrupt()], [Text("Green, sir.")])
    announce = rig.voice.announce

    def wake_then_announce() -> bool:
        rig.audio.wake()
        return announce()

    rig.voice.announce = wake_then_announce  # type: ignore[method-assign]
    rig.clock.advance(1)
    await arrive(rig, arrival)
    await until(lambda: rig.state == "listening")
    rig.voice.announce = announce  # type: ignore[method-assign]
    rig.audio.say("Is the CI green?")
    await until(lambda: "Green, sir." in rig.player.texts and rig.state == "follow_up")
    await rig.advance(7)

    assert talker_kinds(rig)[-2:] == ["event", "user"]
    assert NEWS not in rig.player.texts
    assert rig.states()[-7:-4] == ["idle", "announce", "listening"]
    assert "stop" in rig.player.events
