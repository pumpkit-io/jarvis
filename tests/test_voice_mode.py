"""Voice mode through the Hub: scripted audio input in, the Talker's messages and what was played out."""

import asyncio
import json
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from jarvis_hub.agents.backend import Context, SessionSpec
from jarvis_hub.subagents import Subagent
from jarvis_hub.hub import WARM_UP, Hub
from jarvis_hub.voice.assessment import Assessment, Candidate, SegmentAction, Verdict
from jarvis_hub.voice.state import Heard, SpeechStarted, Timing
from jarvis_hub.voice.voice_mode import VoiceMode, merge
from tests.fakes import (
    AwaitInterrupt,
    FakeBackend,
    FakeClock,
    FakeTts,
    FakeWorkspace,
    Gate,
    Limited,
    RecordingMetrics,
    RecordingOutput,
    RecordingPlayer,
    ResolveScope,
    ScriptedAudio,
    Step,
    Text,
    Tool,
    Typist,
    make_hub,
    subagent_spec,
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
        return [m.split("\n<spoken_scopes>", 1)[0] for s in self.backend.all_sessions("talker")
                for m in s.received if m.startswith("<user")]

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


class FollowUpDecision:
    def __init__(self, action: SegmentAction = "floor") -> None:
        self.action: SegmentAction = action

    async def assess(self, candidate: Candidate, *, current: Callable[[], tuple[str, str]], timeout_s: float = 2.0) -> Assessment:
        return Assessment(candidate.candidate_id, candidate.turn_id, "directed", "scripted", 0,
                          tuple(self.action for _ in candidate.observed_segments))

    async def close(self) -> None:
        pass


@pytest.fixture
async def rig_for() -> AsyncIterator[MakeRig]:
    rigs: list[tuple[Rig, asyncio.Task[None]]] = []

    async def make(*scripts: list[Step], state_path: Path | None = None, recognition: Any = None, workspace: FakeWorkspace | None = None, subagent_session: Callable[[Subagent], SessionSpec] = subagent_spec, **backend_options: Any) -> Rig:
        clock = FakeClock()
        backend = FakeBackend(clock, list(scripts), **backend_options)
        audio, player, output, metrics = ScriptedAudio(clock), RecordingPlayer(), RecordingOutput(), RecordingMetrics()
        tts = FakeTts()
        voice = VoiceMode(
            audio=audio, tts=tts, player=player, clock=clock, timing=TIMING, metrics=metrics, notice=output.notice, recognition=recognition
        )
        hub = make_hub(
            backend,
            output,
            metrics,
            workspace=workspace,
            session=subagent_session,
            speech=voice.speech,
            speak_typed=False,
            on_end_conversation=voice.end_conversation,
            floor=voice,
            state_path=state_path,
        )
        typist = Typist(clock)
        await hub.start()
        voice.start()
        running = asyncio.create_task(hub.run(merge(typist.lines(), voice.lines(hub.interrupt, hub.woke, hub.admit_spoken_revision))))
        rig = Rig(clock, backend, audio, player, output, metrics, typist, voice, hub, tts, running)
        rigs.append((rig, running))
        await until(lambda: player.voices == ["ready"])
        output.log.clear()
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


async def test_delayed_idle_wake_keeps_source_time_and_opens_a_fresh_listening_window(rig_for: MakeRig) -> None:
    from jarvis_hub.voice.state import WakeWord

    rig = await rig_for([Text("The checks passed, sir.")])
    source_at = rig.clock.now()
    await rig.advance(12)
    rig.audio.emit(WakeWord(source_at, delivered_at=rig.clock.now(), owner=0, idle_candidate=1))
    await rig.settle()
    await rig.advance(5.9)
    assert rig.state == "listening"

    rig.audio.emit(SpeechStarted(source_at, owner=1, candidate=1, started_at=source_at))
    rig.audio.emit(Heard("Jarvis, how did the checks finish?", "en", source_at + 2, owner=1, candidate=1))
    await until(lambda: rig.state == "follow_up")

    assert rig.user_messages() == ['<user lang="en">how did the checks finish?</user>']
    assert rig.player.texts == ["", "The checks passed, sir."]


@pytest.mark.parametrize("replacement", ["typed", "announcement"])
async def test_idle_wake_confirmation_cannot_interrupt_a_replacement(rig_for: MakeRig, replacement: str) -> None:
    from jarvis_hub.voice.state import WakeWord

    rig = await rig_for([Text("The build is green, sir.")])
    source_at = rig.clock.now()
    if replacement == "typed":
        rig.typist.type("Check the build")
        await until(lambda: len(rig.user_messages()) == 1)
    else:
        assert rig.voice.announce()
    await rig.advance(1)
    before = rig.player.voices.count("chime")

    rig.audio.emit(WakeWord(source_at, delivered_at=rig.clock.now(), owner=0, idle_candidate=1))
    await rig.settle()

    assert rig.player.voices.count("chime") == before
    assert rig.state == ("idle" if replacement == "typed" else "announce")


async def test_failed_current_wake_handoff_returns_idle_and_allows_a_fresh_wake(rig_for: MakeRig) -> None:
    from jarvis_hub.voice.state import CaptureAborted, WakeWord

    rig = await rig_for([Text("The checks passed, sir.")])
    now = rig.clock.now()
    rig.audio.emit(WakeWord(now, delivered_at=now, owner=0, idle_candidate=1))
    await rig.settle()
    assert rig.state == "listening"

    rig.audio.emit(CaptureAborted(now, 'overflow', owner=1, candidate=1, wake_handoff=True))
    await rig.settle()

    assert rig.state == "idle"
    assert rig.user_messages() == []
    assert any('while waking' in notice for notice in rig.output.notices)
    await asyncio.wait_for(rig.voice.quiet(), 1)
    rig.audio.emit(Heard('lost partial words', 'en', now, owner=1, candidate=2))
    await rig.settle()
    assert rig.user_messages() == []
    await rig.advance(.6)
    rig.audio.wake()
    rig.audio.say('Check the current build')
    await until(lambda: rig.state == "follow_up")
    assert rig.user_messages() == ['<user lang="en">Check the current build</user>']


@pytest.mark.parametrize("source_owner", [1, None])
async def test_stale_wake_handoff_failure_does_not_reset_or_notify_a_replacement(rig_for: MakeRig, source_owner: int | None) -> None:
    from jarvis_hub.voice.state import CaptureAborted, WakeWord

    rig = await rig_for([Text("The replacement is current, sir.")])
    now = rig.clock.now()
    rig.audio.emit(WakeWord(now, delivered_at=now, owner=0, idle_candidate=1))
    await rig.settle()
    await rig.advance(6)
    assert rig.state == "idle"
    rig.audio.wake()
    rig.audio.say('Check the replacement')
    await until(lambda: rig.state == "follow_up")
    notices = rig.output.notices.copy()

    rig.audio.emit(CaptureAborted(now, 'overflow', owner=source_owner, candidate=99, wake_handoff=True))
    await rig.settle()

    assert rig.state == "follow_up"
    assert rig.output.notices == notices
    assert rig.user_messages() == ['<user lang="en">Check the replacement</user>']


async def test_a_follow_up_inside_the_window_needs_no_wake_word(rig_for: MakeRig) -> None:
    rig: Rig = await rig_for([Text("Opened, sir.")], [Text("Green, sir.")], recognition=FollowUpDecision())

    rig.audio.wake()
    rig.audio.say("Open a PR for the login bug")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(4)
    rig.audio.say("And is the CI green?")
    await until(lambda: len(rig.user_messages()) == 2 and rig.state == "follow_up")

    assert rig.user_messages()[-1] == '<user lang="en">Open a PR for the login bug And is the CI green?</user>'
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


@pytest.mark.parametrize(
    ("opening", "answer", "ending"),
    [("Check the CI", "The CI is green, sir.", "That's all."),
     ("Controlla le PR", "Le PR sono pronte, signore.", "Grazie Jarvis.")],
)
async def test_a_whole_utterance_end_control_idles_silently_without_another_model_turn(
    rig_for: MakeRig, opening: str, answer: str, ending: str
) -> None:
    rig: Rig = await rig_for([Text(answer)], recognition=FollowUpDecision("stop"))
    rig.audio.wake()
    rig.audio.say(opening)
    await until(lambda: rig.state == "follow_up")
    played = rig.player.texts.copy()
    turns = len(talker_received(rig))
    rig.audio.say(ending)
    await until(lambda: rig.state == "idle")
    await rig.advance(60)

    assert len(rig.user_messages()) == 1
    assert rig.player.texts == played
    assert len(talker_received(rig)) == turns


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


@pytest.mark.parametrize(
    ("utterance", "farewell", "voice"),
    [("We're done for today", "Until tomorrow, sir.", "am_liam"),
     ("Per oggi abbiamo finito", "A domani, signore.", "im_nicola")],
)
async def test_generated_farewell_finishes_before_idle_without_a_follow_up_window(
    rig_for: MakeRig, utterance: str, farewell: str, voice: str
) -> None:
    rig: Rig = await rig_for([Text(farewell), Tool("end_conversation")])
    rig.player.hold = True
    rig.audio.wake()
    rig.audio.say(utterance)
    await until(lambda: rig.player.texts[-1:] == [farewell])
    await until(lambda: any(t.name == "end_conversation" for t in rig.backend.session("talker").tool_results))
    assert rig.state == "speaking"
    assert rig.player.voices[-1] == voice
    rig.player.finish()
    await rig.advance(0.1)
    await until(lambda: rig.state == "idle")
    await rig.advance(60)

    assert rig.states() == ["listening", "thinking", "speaking", "idle"]
    assert rig.player.texts == ["", farewell]
    assert len(rig.user_messages()) == 1


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
    rig: Rig = await rig_for([Text("Opened, sir."), FULL], [Text("Green, sir."), FULL], recognition=FollowUpDecision())

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
    await until(lambda: "The typo fix is up, sir." in rig.player.texts and rig.state == "follow_up")

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
    # The reply's text is played before its speaking state is stepped, so wait until both have passed.
    await until(lambda: rig.state not in ("thinking", "speaking"))

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
    await until(lambda: "Typed answer, sir." in rig.output.replies)
    await rig.settle()

    assert rig.output.replies == ["Typed answer, sir."]


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


async def news_rig(
    rig_for: MakeRig, *scripts: list[Step], items: int = 1, state_path: Path | None = None, recognition: Any = None
) -> tuple[Rig, list[asyncio.Event]]:
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
    rig: Rig = await rig_for(delegating, *scripts, orchestrator=reports, state_path=state_path, recognition=recognition)
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


async def test_news_that_queued_up_meanwhile_is_one_announcement_in_arrival_order(rig_for: MakeRig) -> None:
    both = "Both are done, sir."
    rig, arrivals = await news_rig(rig_for, [Text("Green, sir.")], [Text(both)], items=2)

    rig.audio.wake()
    await until(lambda: rig.state == "listening")
    for arrival in arrivals:
        await arrive(rig, arrival)
    rig.audio.say("Is the CI green?")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(6)
    await until(lambda: both in rig.player.texts and rig.state == "follow_up")

    assert talker_kinds(rig)[-2:] == ["user", "event"]
    news = rig.backend.session("talker").received[-1].splitlines()
    assert [line[: line.index(">") + 1] for line in news] == [
        '<event source="orchestrator" task="T1" needs_user="false">',
        '<event source="orchestrator" task="T2" needs_user="false">',
    ]
    assert rig.player.texts[-3:] == ["Green, sir.", "", both]
    await rig.advance(4)
    assert rig.state == "idle"
    assert talker_kinds(rig)[-1] == "event"


async def test_a_wake_cuts_unheard_news_and_the_news_after_it_still_plays(rig_for: MakeRig) -> None:
    rig, arrivals = await news_rig(rig_for, [Text(NEWS)], [Text("Green, sir.")], [Text(DOCS)], items=2)

    rig.tts.hold.clear()
    await arrive(rig, arrivals[0])
    await until(lambda: rig.output.replies[-1:] == [NEWS])
    await arrive(rig, arrivals[1])
    await rig.advance(1)
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


async def test_a_queued_spoken_turn_superseded_by_wake_is_not_admitted(rig_for: MakeRig) -> None:
    typed_done = asyncio.Event()
    rig: Rig = await rig_for(
        [Gate(typed_done), Text("Typed answer, sir.")], [Text("Green, sir.")]
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

    assert "Is main green?" not in "\n".join(rig.user_messages())
    assert "Main is red, sir." not in rig.output.replies


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


async def test_news_from_a_check_that_comes_due_mid_exchange_waits_until_jarvis_is_quiet(rig_for: MakeRig) -> None:
    check = {"after_seconds": 60, "note": "Has Ada reviewed pumpkit#42?", "max_repeats": 1}
    report = {"task_id": "T1", "spoken": "Ada approved pumpkit 42.", "needs_user": False}
    rig: Rig = await rig_for(
        [Text("I'll keep an eye on it, sir."), Tool("delegate", {"request": "Tell me when Ada reviews", "context": ""})],
        [Text("Green, sir.")],
        [Text("Ada has approved it, sir.")],
        orchestrator=[[Tool("schedule_check", check)], [Tool("report", report)]],
    )

    rig.audio.wake()
    rig.audio.say("Tell me when Ada reviews pumpkit 42")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(6)
    assert rig.state == "idle"
    await rig.advance(50)
    rig.audio.wake()
    rig.audio.say("Is the CI green?")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(4)
    await until(lambda: len(rig.backend.session("orchestrator").received) == 2)
    await rig.settle()
    assert rig.backend.session("orchestrator").received[1].startswith('<event source="timer" id="C1" task="T1"')
    assert rig.state == "follow_up" and "Ada has approved it, sir." not in rig.output.replies

    await rig.advance(2)
    await until(lambda: "Ada has approved it, sir." in rig.player.texts and rig.state == "follow_up")
    assert spoken(rig)[-2:] == ["Green, sir.", "Ada has approved it, sir."]
    assert rig.states()[-3:] == ["idle", "announce", "follow_up"]


async def test_news_while_idle_chimes_then_says_its_sentence_and_listens_briefly(rig_for: MakeRig) -> None:
    rig, [arrival] = await news_rig(rig_for, [Text(NEWS)])
    rig.player.clips.clear()

    await arrive(rig, arrival)
    await until(lambda: NEWS in rig.player.texts and rig.state == "follow_up")

    assert rig.player.voices[:2] == ["chime", "am_liam"]
    assert rig.player.texts[:2] == ["", NEWS]
    await rig.advance(3.9)
    assert rig.state == "follow_up"
    await rig.advance(0.1)
    assert rig.state == "idle"


QUIET: list[Step] = [Text("Very well, sir."), Tool("quiet_mode", {"on": True})]
AWAY = "Two updates came in, sir."


async def quiet_rig(rig_for: MakeRig, *scripts: list[Step], items: int = 2) -> tuple[Rig, list[asyncio.Event]]:
    """`news_rig`, then the User asks for Quiet mode and Jarvis is idle again."""
    rig, arrivals = await news_rig(rig_for, QUIET, *scripts, items=items)
    rig.audio.wake()
    rig.audio.say("Quiet mode, please")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(6)
    assert rig.state == "idle"
    return rig, arrivals


def talker_received(rig: Rig) -> list[str]:
    return [m for s in rig.backend.all_sessions("talker") for m in s.received]


async def test_quiet_mode_holds_news_until_the_wake_word_which_opens_with_the_count(rig_for: MakeRig) -> None:
    rig, arrivals = await quiet_rig(rig_for, [Text(AWAY), Text(" Main is green, sir.")])
    turns = len(talker_received(rig))

    for arrival in arrivals:
        await arrive(rig, arrival)
    await rig.advance(60)
    assert len(talker_received(rig)) == turns
    assert rig.state == "idle"

    rig.audio.wake()
    rig.audio.say("Is main green?")
    await until(lambda: rig.state == "follow_up")

    [message] = talker_received(rig)[turns:]
    lines = message.splitlines()
    assert lines[0] == '<event source="hub" away="2">2 updates while you were away; their details are on screen.</event>'
    assert lines[1].startswith('<event source="orchestrator" task="T1"')
    assert lines[2].startswith('<event source="orchestrator" task="T2"')
    assert lines[3] == '<user lang="en">Is main green?</user>'
    assert talker_kinds(rig)[-1] == "user"
    [shown] = rig.output.shown
    assert "T1: The typo fix is up." in shown and "T2: The docs are merged." in shown
    assert spoken(rig)[-1] == "Main is green, sir."
    assert spoken(rig)[-2] == AWAY


async def test_a_wake_with_nothing_said_after_quiet_mode_announces_the_count_once_idle(rig_for: MakeRig) -> None:
    rig, arrivals = await quiet_rig(rig_for, [Text(AWAY)])
    for arrival in arrivals:
        await arrive(rig, arrival)

    rig.audio.wake()
    await until(lambda: rig.state == "listening")
    await rig.advance(6)
    await until(lambda: AWAY in rig.player.texts and rig.state == "follow_up")

    assert talker_kinds(rig)[-1] == "event"
    assert talker_received(rig)[-1].startswith('<event source="hub" away="2">2 updates while you were away')
    assert rig.states()[-3:] == ["idle", "announce", "follow_up"]


async def test_quiet_mode_ends_at_the_wake_word_so_later_news_is_announced(rig_for: MakeRig) -> None:
    rig, [arrival] = await quiet_rig(rig_for, [Text("Main is green, sir.")], [Text(NEWS)], items=1)

    rig.audio.wake()
    rig.audio.say("Is main green?")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(6)
    await arrive(rig, arrival)
    await until(lambda: NEWS in rig.player.texts and rig.state == "follow_up")

    assert not any("away=" in m for m in talker_received(rig))


async def test_quiet_mode_holds_news_that_was_already_waiting_for_the_reply_to_end(rig_for: MakeRig) -> None:
    rig, [arrival] = await news_rig(rig_for, QUIET, [Text("One update while you were away, sir.")])

    rig.audio.wake()
    await until(lambda: rig.state == "listening")
    await arrive(rig, arrival)
    rig.audio.say("Quiet mode, please")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(60)
    assert talker_kinds(rig)[-1] == "user"

    rig.audio.wake()
    await rig.advance(6)
    await until(lambda: rig.state == "follow_up")
    assert talker_received(rig)[-1].startswith('<event source="hub" away="1">1 update while you were away')


async def test_quiet_mode_off_by_a_typed_line_announces_what_was_held(rig_for: MakeRig) -> None:
    off: list[Step] = [Text("Quiet mode is off, sir."), Tool("quiet_mode", {"on": False})]
    rig, arrivals = await quiet_rig(rig_for, off, [Text(AWAY)])
    for arrival in arrivals:
        await arrive(rig, arrival)

    rig.typist.type("Quiet mode off")
    await until(lambda: AWAY in rig.player.texts and rig.state == "follow_up")

    assert talker_kinds(rig)[-2:] == ["user", "event"]
    assert talker_received(rig)[-1].startswith('<event source="hub" away="2">')


async def test_news_held_in_quiet_mode_is_shown_when_the_input_ends_and_the_hub_stops(rig_for: MakeRig) -> None:
    rig, arrivals = await quiet_rig(rig_for, [Text(AWAY)])
    for arrival in arrivals:
        await arrive(rig, arrival)

    rig.typist.end()
    await rig.advance(30)

    await until(lambda: rig.running.done())
    assert AWAY in rig.output.replies
    assert AWAY not in rig.player.texts


RED = "CI is red on pumpkit 42."


async def blocker_rig(
    rig_for: MakeRig, reports_: list[tuple[str, bool]], *scripts: list[Step], state_path: Path | None = None
) -> tuple[Rig, list[asyncio.Event]]:
    """One Task whose Orchestrator turn sends `reports_` (spoken, needs_user) one per arrival, in Quiet mode."""
    arrivals = [asyncio.Event() for _ in reports_]
    turn: list[Step] = []
    for arrival, (said, needs_user) in zip(arrivals, reports_):
        turn += [Gate(arrival), Tool("report", {"task_id": "T1", "spoken": said, "needs_user": needs_user})]
    delegating = [Text("Right away, sir."), Tool("delegate", {"request": "Watch the CI", "context": ""})]
    rig: Rig = await rig_for(delegating, QUIET, *scripts, orchestrator=[turn], state_path=state_path)
    for said in ("Watch the CI on pumpkit 42", "Quiet mode, please"):
        rig.audio.wake()
        rig.audio.say(said)
        await until(lambda: rig.state == "follow_up")
        await rig.advance(6)
    assert rig.state == "idle"
    return rig, arrivals


async def test_news_that_needs_the_user_gets_through_quiet_mode_once_per_change(rig_for: MakeRig) -> None:
    worse = "CI is red on pumpkit 42 with a new failure."
    rig, arrivals = await blocker_rig(
        rig_for, [(RED, True), (RED, True), (worse, True)], [Text("CI is red, sir.")], [Text("A new failure, sir.")]
    )

    await arrive(rig, arrivals[0])
    await until(lambda: "CI is red, sir." in rig.player.texts and rig.state == "follow_up")
    assert rig.player.texts[-2:] == ["", "CI is red, sir."]
    await rig.advance(4)
    turns = len(talker_received(rig))

    await arrive(rig, arrivals[1])
    await rig.advance(60)
    assert len(talker_received(rig)) == turns

    await arrive(rig, arrivals[2])
    await until(lambda: "A new failure, sir." in rig.player.texts and rig.state == "follow_up")
    assert talker_received(rig)[-1].startswith(f'<event source="orchestrator" task="T1" needs_user="true">{worse}')


async def test_needs_user_news_counts_as_changed_after_the_task_reported_something_else(rig_for: MakeRig) -> None:
    rig, arrivals = await blocker_rig(
        rig_for,
        [(RED, True), ("CI is green.", False), (RED, True)],
        [Text("CI is red, sir.")],
        [Text("Red again, sir.")],
    )

    await arrive(rig, arrivals[0])
    await until(lambda: "CI is red, sir." in rig.player.texts and rig.state == "follow_up")
    await rig.advance(4)
    await arrive(rig, arrivals[1])
    await rig.advance(30)
    assert "Red again, sir." not in rig.player.texts

    await arrive(rig, arrivals[2])
    await until(lambda: "Red again, sir." in rig.player.texts and rig.state == "follow_up")
    assert talker_received(rig)[-1].startswith('<event source="orchestrator" task="T1" needs_user="true">')


async def test_a_repeat_held_in_quiet_mode_still_counts_among_the_updates(rig_for: MakeRig) -> None:
    rig, arrivals = await blocker_rig(
        rig_for, [(RED, True), (RED, True)], [Text("CI is red, sir.")], [Text("One more.")]
    )
    await arrive(rig, arrivals[0])
    await until(lambda: "CI is red, sir." in rig.player.texts and rig.state == "follow_up")
    await rig.advance(4)
    await arrive(rig, arrivals[1])

    rig.audio.wake()
    await rig.advance(6)
    await until(lambda: "One more." in rig.player.texts)
    assert talker_received(rig)[-1].startswith('<event source="hub" away="1">1 update while you were away')


def crash(state: Path, tmp_path: Path) -> Path:
    """The state file as a crash would leave it now, for a second Hub to start from."""
    crashed = tmp_path / "crashed" / "state.json"
    crashed.parent.mkdir(exist_ok=True)
    shutil.copy(state, crashed)
    return crashed


async def test_news_held_in_quiet_mode_survives_a_crash_and_opens_the_turn_after_the_next_wake_word(
    rig_for: MakeRig, tmp_path: Path
) -> None:
    state = tmp_path / ".jarvis" / "state.json"
    first, arrivals = await news_rig(rig_for, QUIET, items=2, state_path=state)
    first.audio.wake()
    first.audio.say("Quiet mode, please")
    await until(lambda: first.state == "follow_up")
    await first.advance(6)
    for arrival in arrivals:
        await arrive(first, arrival)

    second = await rig_for([Text(AWAY), Text(" Main is green, sir.")], state_path=crash(state, tmp_path))
    # The warm-up turn runs in the background, so it is counted once it has gone to the Talker.
    await until(lambda: talker_received(second) != [])
    turns = len(talker_received(second))
    await second.advance(60)
    assert len(talker_received(second)) == turns

    second.audio.wake()
    second.audio.say("Is main green?")
    await until(lambda: second.state == "follow_up")
    [message] = talker_received(second)[turns:]
    lines = message.splitlines()
    assert lines[0].startswith('<event source="hub" away="2">2 updates while you were away')
    assert lines[1].startswith('<event source="orchestrator" task="T1"')
    assert lines[2].startswith('<event source="orchestrator" task="T2"')
    assert lines[3] == '<user lang="en">Is main green?</user>'


async def test_after_a_restart_quiet_mode_holds_a_recovery_report_that_repeats_the_last_needs_user_news(
    rig_for: MakeRig, tmp_path: Path
) -> None:
    state = tmp_path / ".jarvis" / "state.json"
    first, [arrival] = await blocker_rig(rig_for, [(RED, True)], [Text("CI is red, sir.")], state_path=state)
    await arrive(first, arrival)
    await until(lambda: "CI is red, sir." in first.player.texts and first.state == "follow_up")
    await first.advance(4)

    again = Tool("report", {"task_id": "T1", "spoken": RED, "needs_user": True})
    second = await rig_for(
        [Text("One update, sir.")], state_path=crash(state, tmp_path), orchestrator=[[again]]
    )
    # The warm-up turn runs in the background, so it is counted once it has gone to the Talker.
    await until(lambda: talker_received(second) != [])
    turns = len(talker_received(second))
    await until(lambda: second.backend.all_sessions("orchestrator") != [])
    await second.advance(60)
    assert len(talker_received(second)) == turns

    second.audio.wake()
    await second.advance(6)
    await until(lambda: "One update, sir." in second.player.texts)
    assert talker_received(second)[-1].startswith('<event source="hub" away="1">1 update while you were away')


async def test_a_recovery_report_outside_quiet_mode_is_an_announcement_once_jarvis_is_idle(
    rig_for: MakeRig, tmp_path: Path
) -> None:
    state = tmp_path / ".jarvis" / "state.json"
    _, [arrival] = await news_rig(rig_for, items=1, state_path=state)
    recovered = asyncio.Event()
    fixed = Tool("report", {"task_id": "T1", "spoken": "The typo fix is up.", "needs_user": False})
    second = await rig_for(
        [Text(NEWS)], state_path=crash(state, tmp_path), orchestrator=[[Gate(recovered), fixed]]
    )
    recovered.set()
    await until(lambda: NEWS in second.player.texts and second.state == "follow_up")

    assert second.player.texts[-2:] == ["", NEWS]
    assert second.states()[-2:] == ["announce", "follow_up"]
    arrival.set()


async def test_the_usage_limit_reset_is_held_in_quiet_mode_and_counted_with_the_news_that_waited_for_it(
    rig_for: MakeRig,
) -> None:
    limit_line = "Model unavailable: usage limit."
    delegating = [Text("Right away, sir."), Tool("delegate", {"request": "Check the CI", "context": ""})]
    report = Tool("report", {"task_id": "T1", "spoken": "The CI is green.", "needs_user": False})
    rig: Rig = await rig_for(delegating, QUIET, [Text(AWAY)], orchestrator=[[Limited(600)], [report]])
    rig.audio.wake()
    rig.audio.say("Check the CI")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(6)
    # Limit status stays on screen while the voice remains available.
    await until(lambda: any(limit_line in n for n in rig.output.notices))
    await rig.advance(10)
    rig.audio.wake()
    rig.audio.say("Quiet mode, please")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(6)
    assert rig.state == "idle"
    turns = len(talker_received(rig))

    await rig.advance(600)
    await until(lambda: reports(rig) == 1)
    await rig.advance(60)
    assert len(talker_received(rig)) == turns

    rig.audio.wake()
    await rig.advance(6)
    await until(lambda: AWAY in rig.player.texts)
    lines = talker_received(rig)[-1].splitlines()
    assert lines[0].startswith('<event source="hub" away="2">2 updates while you were away')
    assert sorted(line.split()[1] for line in lines[1:]) == ['source="hub"', 'source="orchestrator"']
    assert any('reason="usage_reset"' in line for line in lines[1:])


async def test_a_new_limit_drops_the_reset_news_held_in_quiet_mode_so_it_is_never_told_while_limited(
    rig_for: MakeRig,
) -> None:
    limit_line = "Model unavailable: usage limit."
    delegating = [Text("Right away, sir."), Tool("delegate", {"request": "Check the CI", "context": ""})]
    again = [Text("Once more, sir."), Tool("delegate", {"request": "Check again", "context": ""})]
    report = Tool("report", {"task_id": "T1", "spoken": "The CI is green.", "needs_user": False})
    rig: Rig = await rig_for(
        delegating, QUIET, again, [Text(AWAY)], orchestrator=[[Limited(600)], [report], [Limited(600)]]
    )
    rig.audio.wake()
    rig.audio.say("Check the CI")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(6)
    await until(lambda: any(limit_line in n for n in rig.output.notices))
    await rig.advance(10)
    rig.audio.wake()
    rig.audio.say("Quiet mode, please")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(6)
    await rig.advance(600)
    await until(lambda: reports(rig) == 1)
    await rig.advance(60)
    notices_before = sum(limit_line in n for n in rig.output.notices)

    rig.typist.type("Check again")
    await until(lambda: sum(limit_line in n for n in rig.output.notices) == notices_before + 1)
    await rig.advance(60)
    rig.audio.wake()
    await rig.advance(6)
    await until(lambda: AWAY in rig.player.texts)

    lines = talker_received(rig)[-1].splitlines()
    assert lines[0].startswith('<event source="hub" away="1">1 update while you were away')
    assert not any('reason="usage_reset"' in m for m in talker_received(rig))


async def test_news_told_in_one_announcement_with_the_reset_is_still_told_when_the_talker_hits_the_limit_mid_way(
    rig_for: MakeRig,
) -> None:
    limit_line = "Model unavailable: usage limit."
    rig: Rig = await rig_for([Text("Hello, sir.")], [Limited(600)], [Text("The CI is green, sir.")])
    rig.audio.wake()
    rig.audio.say("Hello")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(6)
    assert rig.state == "idle"

    # The reset first, then a real report: one Announcement, which the Talker cannot finish.
    rig.hub._limit_reset()  # pyright: ignore[reportPrivateUsage]
    rig.hub._post_news("orchestrator", "The CI is green.", "T1")  # pyright: ignore[reportPrivateUsage]
    await until(lambda: len(talker_received(rig)) == 2)
    await until(lambda: any(limit_line in n for n in rig.output.notices))
    await rig.advance(600)
    await until(lambda: "The CI is green, sir." in rig.player.texts)

    told = [m for m in talker_received(rig) if 'source="orchestrator"' in m]
    assert len(told) == 2
    assert 'reason="usage_reset"' in told[0]
    assert 'reason="usage_reset"' not in told[1]


LIMIT_LINE = "Model unavailable: usage limit."
DOCS_REPORT = "The docs are merged."


def red_then_docs(arrival: asyncio.Event) -> list[Step]:
    """One Orchestrator turn reporting news that needs the User, then ordinary news, once `arrival` is set."""
    return [
        Gate(arrival),
        Tool("report", {"task_id": "T1", "spoken": RED, "needs_user": True}),
        Tool("report", {"task_id": "T1", "spoken": DOCS_REPORT, "needs_user": False}),
    ]


async def watch_the_ci(rig: Rig, arrival: asyncio.Event) -> None:
    """The User delegates; once Jarvis is idle both reports arrive and go out as one Announcement."""
    rig.audio.wake()
    rig.audio.say("Watch the CI")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(6)
    arrival.set()
    await until(lambda: reports(rig) == 2)


def away_turn(message: str) -> tuple[int, list[str]]:
    """The count an away opener says, and the events it lists after it."""
    first, *rest = message.splitlines()
    assert first.startswith('<event source="hub" away="')
    return int(first.split('"')[3]), [line for line in rest if line.startswith("<event")]


async def test_quiet_mode_lets_through_only_the_urgent_news_of_a_turn_that_waited_for_the_reset(
    rig_for: MakeRig,
) -> None:
    arrival = asyncio.Event()
    delegating = [Text("Right away, sir."), Tool("delegate", {"request": "Watch the CI", "context": ""})]
    rig: Rig = await rig_for(
        delegating,
        [Limited(600)],
        QUIET,
        [Text("CI is red, sir.")],
        [Text(AWAY)],
        orchestrator=[red_then_docs(arrival)],
    )
    await watch_the_ci(rig, arrival)
    await until(lambda: any(LIMIT_LINE in n for n in rig.output.notices))
    limited_at = rig.clock.t

    # The User takes the floor just before the hold clears, so the parked turn waits for them.
    await rig.advance(limited_at + 597 - rig.clock.t)
    rig.audio.wake()
    await until(lambda: rig.state == "listening")
    await rig.advance(4)
    await until(lambda: any(r["kind"] == "usage_reset" for r in rig.metrics.records))
    rig.audio.say("Quiet mode, please")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(6)
    await until(lambda: "CI is red, sir." in rig.player.texts)

    told = talker_received(rig)[-1]
    assert RED in told and DOCS_REPORT not in told

    await until(lambda: rig.state == "follow_up")
    await rig.advance(4)
    rig.audio.wake()
    await rig.advance(6)
    await until(lambda: AWAY in rig.player.texts)
    count, events = away_turn(talker_received(rig)[-1])
    assert count == len(events)
    assert any(DOCS_REPORT in e for e in events) and not any(RED in e for e in events)


async def test_a_parked_turn_restored_in_quiet_mode_lets_through_only_its_urgent_news(
    rig_for: MakeRig, tmp_path: Path
) -> None:
    state = tmp_path / ".jarvis" / "state.json"
    arrival = asyncio.Event()
    delegating = [Text("Right away, sir."), Tool("delegate", {"request": "Watch the CI", "context": ""})]
    # The Talker turns Quiet mode on in the Announcement and then hits the limit: the whole turn is parked.
    quiet_then_limited: list[Step] = [Tool("quiet_mode", {"on": True}), Limited(600)]
    first: Rig = await rig_for(
        delegating, quiet_then_limited, orchestrator=[red_then_docs(arrival)], state_path=state
    )
    await watch_the_ci(first, arrival)
    await until(lambda: any(LIMIT_LINE in n for n in first.output.notices))
    await first.settle()

    second = await rig_for([Text("CI is red, sir.")], [Text(AWAY)], state_path=crash(state, tmp_path))
    await until(lambda: talker_received(second) != [])
    await second.advance(700)
    await until(lambda: "CI is red, sir." in second.player.texts)

    told = talker_received(second)[-1]
    assert RED in told and DOCS_REPORT not in told

    await until(lambda: second.state == "follow_up")
    await second.advance(4)
    second.audio.wake()
    await second.advance(6)
    await until(lambda: AWAY in second.player.texts)
    count, events = away_turn(talker_received(second)[-1])
    assert count == len(events)
    assert any(DOCS_REPORT in e for e in events) and not any(RED in e for e in events)


async def parked_away(rig_for: MakeRig, tmp_path: Path) -> Path:
    """A Hub whose "while you were away" Announcement of two held items hit the limit after turning Quiet mode back
    on; returns the state file a crash left."""
    state = tmp_path / ".jarvis" / "state.json"
    quiet_then_limited: list[Step] = [Tool("quiet_mode", {"on": True}), Limited(600)]
    first, arrivals = await news_rig(rig_for, QUIET, quiet_then_limited, items=2, state_path=state)
    first.audio.wake()
    first.audio.say("Quiet mode, please")
    await until(lambda: first.state == "follow_up")
    await first.advance(6)
    for arrival in arrivals:
        await arrive(first, arrival)
    first.audio.wake()
    await first.advance(6)
    await until(lambda: any(LIMIT_LINE in n for n in first.output.notices))
    await first.settle()
    return crash(state, tmp_path)


async def test_a_parked_away_announcement_restored_in_quiet_mode_lists_each_item_it_held(
    rig_for: MakeRig, tmp_path: Path
) -> None:
    second = await rig_for([Text(AWAY)], state_path=await parked_away(rig_for, tmp_path))
    await until(lambda: talker_received(second) != [])
    await second.advance(700)

    second.audio.wake()
    await second.advance(6)
    await until(lambda: AWAY in second.player.texts)
    count, events = away_turn(talker_received(second)[-1])
    assert count == len(events) == 2
    assert [e.split('"')[3] for e in events] == ["T1", "T2"]
    shown = second.output.shown[-1]
    assert "- T1: The typo fix is up.\n- T2: The docs are merged." in shown
    assert "None" not in shown


async def test_two_restarts_keep_the_exact_count_of_the_updates_held(rig_for: MakeRig, tmp_path: Path) -> None:
    restored = await parked_away(rig_for, tmp_path)
    second = await rig_for(state_path=restored)
    await until(lambda: talker_received(second) != [])
    await second.advance(700)
    await second.settle()

    (tmp_path / "again").mkdir()
    third = await rig_for([Text(AWAY)], [Text("Noted, sir.")], state_path=crash(restored, tmp_path / "again"))
    await until(lambda: talker_received(third) != [])
    third.audio.wake()
    await third.advance(6)
    await until(lambda: AWAY in third.player.texts)
    count, events = away_turn(talker_received(third)[-1])
    assert count == len(events)
    assert [e.split('"')[3] for e in events if 'source="orchestrator"' in e] == ["T1", "T2"]


STORY = [Text("First sentence, sir. "), Text("Second sentence follows. "), Text("The third one ends it. ")]


async def speaking(rig: Rig) -> None:
    """Jarvis is half a second into its reply, past the Barge-in guard, with its clips queued and none played out.

    The recording player plays nothing on its own, and a reply that makes no progress for 2 s counts as done.
    """
    rig.audio.wake()
    await until(lambda: rig.player.voices[-1:] == ["chime"])
    rig.player.hold = True
    rig.audio.say("Tell me a story")
    await until(lambda: rig.state == "speaking" and len(rig.player.clips) >= 3)
    # The final sentence's trailing space closes it even if the model is still running.
    await rig.advance(0.5)
    assert len(rig.player.clips) == 4


def talker_got(rig: Rig) -> list[str]:
    return [m for m in rig.backend.session("talker").received if m != WARM_UP]


async def test_okay_over_jarvis_pauses_it_and_it_resumes_a_little_back_within_a_second(rig_for: MakeRig) -> None:
    rig = await rig_for(STORY)
    await speaking(rig)

    rig.audio.barge()
    await until(lambda: rig.player.events[-1] == "pause")
    await rig.advance(0.2)
    rig.audio.partial("Okay")
    await rig.advance(0.3)
    assert rig.player.events[-1] == "pause"
    rig.audio.partial("Okay.", silent=0.35)
    await until(lambda: rig.player.events[-1] == "resume 300")

    # 0.7 s from the onset of "okay", and the Backchannel is no turn.
    assert rig.state == "speaking"
    rig.player.finish()
    await rig.advance(0.1)
    assert rig.state == "follow_up"
    assert talker_got(rig) == ['<user lang="en">Tell me a story</user>']


async def test_mhm_twice_resumes_twice(rig_for: MakeRig) -> None:
    rig = await rig_for(STORY)
    await speaking(rig)

    for _ in range(2):
        rig.audio.barge()
        await rig.advance(0.5)
        rig.audio.partial("Mhm.", silent=0.3)
        await rig.settle()
        rig.player.finish(1)
        await rig.advance(1.0)

    assert [e for e in rig.player.events if not e.startswith("play")] == ["pause", "resume 300"] * 2
    assert rig.state == "speaking"
    assert rig.audio.watching[-1] is not None


async def test_a_cough_pauses_jarvis_only_briefly(rig_for: MakeRig) -> None:
    rig = await rig_for(STORY)
    await speaking(rig)

    rig.audio.barge(held=0.25)
    await rig.advance(0.15)
    rig.audio.partial("", silent=0.25)
    await until(lambda: rig.player.events[-1] == "resume 300")

    assert rig.state == "speaking"
    assert "stop" not in rig.player.events


async def test_wait_stop_stops_jarvis_and_listens_and_the_turn_says_what_was_heard(rig_for: MakeRig) -> None:
    rig = await rig_for([*STORY, AwaitInterrupt()], [Text("Of course, sir.")])
    await speaking(rig)
    rig.player.finish(1)

    rig.audio.barge()
    await rig.advance(0.2)
    rig.audio.partial("Wait, stop")
    await until(lambda: rig.state == "listening")

    assert rig.player.events[-2:] == ["pause", "stop"]
    assert rig.player.voices[-1] != "chime"
    talker = rig.backend.session("talker")
    await until(lambda: not talker._sending)  # pyright: ignore[reportPrivateUsage]
    assert rig.audio.capturing[-1] is True and rig.audio.watching[-1] is None

    rig.audio.say("Wait, stop.")
    await until(lambda: len(talker_got(rig)) == 2)
    assert talker_got(rig)[1] == '<interrupted spoken="First sentence, sir."/>\n<user lang="en">Wait, stop.</user>'


async def test_a_new_request_over_jarvis_stops_it_and_is_answered_with_what_was_heard(rig_for: MakeRig) -> None:
    rig = await rig_for([*STORY, AwaitInterrupt()], [Text("Right away, sir.")])
    await speaking(rig)
    rig.player.finish(1)

    rig.audio.barge()
    await rig.advance(0.2)
    rig.audio.partial("Actually, open")
    await until(lambda: rig.state == "listening")
    rig.audio.say("Actually, open an issue instead")
    await until(lambda: rig.state == "speaking" and rig.player.texts[-1] == "Right away, sir.")

    assert talker_got(rig)[1] == (
        '<interrupted spoken="First sentence, sir."/>\n<user lang="en">Actually, open an issue instead</user>'
    )
    assert "Second sentence follows." not in rig.player.texts[rig.player.texts.index("Right away, sir.") :]


async def test_jarvis_said_over_a_reply_always_stops_it(rig_for: MakeRig) -> None:
    rig = await rig_for([*STORY, AwaitInterrupt()], [Text("Two Subagents, sir.")])
    await speaking(rig)

    rig.audio.barge()
    await rig.advance(0.1)
    rig.audio.wake()
    await until(lambda: rig.state == "listening" and rig.player.voices[-1] == "chime")
    rig.audio.say("What is running?")
    await until(lambda: len(talker_got(rig)) == 2)

    assert "stop" in rig.player.events
    assert talker_got(rig)[1] == '<interrupted spoken=""/>\n<user lang="en">What is running?</user>'


async def test_speech_inside_the_guard_is_ignored_and_a_later_barge_in_still_stops_jarvis(rig_for: MakeRig) -> None:
    rig = await rig_for([*STORY, AwaitInterrupt()])
    rig.audio.wake()
    await until(lambda: rig.player.voices[-1:] == ["chime"])
    rig.player.hold = True
    rig.audio.say("Tell me a story")
    await until(lambda: rig.state == "speaking")
    started = rig.clock.now()
    assert rig.audio.watching[-1] == pytest.approx(started + 0.25)  # pyright: ignore[reportUnknownMemberType]

    rig.audio.barge(held=0.3)
    await rig.advance(0.1)
    rig.audio.partial("Stop")
    await rig.advance(0.4)
    assert "pause" not in rig.player.events and rig.state == "speaking"

    rig.audio.barge()
    await until(lambda: rig.player.events[-1:] == ["pause"])
    rig.audio.partial("Stop")
    await until(lambda: rig.state == "listening")
    assert "stop" in rig.player.events


async def test_barge_in_is_watched_only_while_jarvis_speaks(rig_for: MakeRig) -> None:
    rig = await rig_for([Text("Hi, sir.")])

    rig.audio.wake()
    rig.audio.say("Hello")
    await until(lambda: rig.state == "follow_up")

    assert [since is not None for since in rig.audio.watching] == [True, False]


def barge_rows(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.metrics.records if r["kind"] == "bargein"]


async def test_a_backchannel_resume_is_logged_with_its_partials_timing_and_what_had_been_said(rig_for: MakeRig) -> None:
    rig = await rig_for(STORY)
    await speaking(rig)
    since = rig.audio.watching[-1]
    assert since is not None

    rig.audio.barge()
    await until(lambda: rig.player.events[-1] == "pause")
    await rig.advance(0.2)
    rig.audio.partial("Okay.", silent=0.35)
    await until(lambda: rig.player.events[-1] == "resume 300")
    # A resume waits to learn whether the User repeats themselves.
    assert barge_rows(rig) == []
    await rig.voice.close()

    [row] = barge_rows(rig)
    assert row["since"] == pytest.approx(since, abs=1e-3)  # pyright: ignore[reportUnknownMemberType]
    assert row["onset"] >= row["since"]
    assert row["decision"] == "resume" and row["extended"] is False and row["wake_hits"] == []
    assert row["detected_ms"] == 200
    assert [p["text"] for p in row["partials"]] == ["Okay."]
    assert row["partials"][0]["ms"] == row["decided_ms"] == 400
    assert row["played"] == rig.player.played()
    assert (row["repeated"], row["next"]) == (False, None)


async def test_wait_stop_is_logged_as_a_stop_with_no_repeat_to_judge(rig_for: MakeRig) -> None:
    rig = await rig_for([*STORY, AwaitInterrupt()], [Text("Of course, sir.")])
    await speaking(rig)

    rig.audio.barge()
    await rig.advance(0.2)
    rig.audio.partial("Wait, stop")
    await until(lambda: rig.state == "listening")

    [row] = barge_rows(rig)
    assert (row["decision"], row["decided_ms"], row["repeated"], row["next"]) == ("stop", 400, None, None)


async def test_the_wake_word_over_a_barge_in_is_logged_with_its_hit(rig_for: MakeRig) -> None:
    rig = await rig_for([*STORY, AwaitInterrupt()], [Text("Two Subagents, sir.")])
    await speaking(rig)

    rig.audio.barge()
    await rig.advance(0.1)
    rig.audio.wake()
    await until(lambda: rig.state == "listening")

    [row] = barge_rows(rig)
    assert (row["decision"], row["wake_hits"], row["partials"]) == ("wake", [300], [])


async def test_a_decision_that_waited_past_its_deadline_for_one_more_partial_is_logged_as_extended(
    rig_for: MakeRig,
) -> None:
    rig = await rig_for(STORY)
    await speaking(rig)
    rig.player.finish(1)

    rig.audio.barge()
    await rig.advance(0.2)
    rig.audio.partial("Okay")
    await rig.advance(0.7)
    assert rig.player.events[-1] == "pause"
    await rig.advance(0.6)
    await rig.voice.close()

    [row] = barge_rows(rig)
    assert (row["decision"], row["extended"]) == ("resume", True)


async def test_a_stop_by_an_interrupt_said_after_the_runs_last_barge_in_is_logged_as_overheard(
    rig_for: MakeRig,
) -> None:
    rig = await rig_for([*STORY, AwaitInterrupt()], [Text("Of course, sir.")])
    await speaking(rig)

    rig.audio.overheard("okay okay okay")
    await rig.advance(0.3)
    assert barge_rows(rig) == []
    rig.audio.overheard("okay okay okay wait stop")
    await until(lambda: rig.state == "listening")

    [row] = barge_rows(rig)
    assert row["decision"] == "overheard-stop" and row["onset"] is None
    assert [p["text"] for p in row["partials"]] == ["okay okay okay wait stop"]


async def resumed_over(rig: Rig, text: str = "Okay.") -> None:
    rig.audio.barge()
    await rig.advance(0.2)
    rig.audio.partial(text, silent=0.35)
    await until(lambda: rig.player.events[-1] == "resume 300")


async def test_a_new_barge_in_after_a_quiet_resume_marks_it_a_likely_wrong_resume(rig_for: MakeRig) -> None:
    rig = await rig_for([*STORY, AwaitInterrupt()])
    await speaking(rig)
    await resumed_over(rig, "")
    await rig.advance(1.0)

    rig.audio.barge()
    await until(lambda: len(barge_rows(rig)) == 1)

    [row] = barge_rows(rig)
    assert (row["decision"], row["repeated"], row["next"]) == ("resume", True, "bargein")


async def test_speech_that_ran_on_through_the_resume_is_one_run_and_not_a_repeat(rig_for: MakeRig) -> None:
    rig = await rig_for([*STORY, AwaitInterrupt()])
    await speaking(rig)
    rig.audio.barge()
    await rig.advance(0.2)
    rig.audio.partial("Okay okay")
    await rig.advance(0.7)
    await rig.advance(0.6)
    await until(lambda: rig.player.events[-1] == "resume 300")

    rig.audio.barge()
    await rig.advance(0.1)
    await rig.voice.close()

    assert [(r["decision"], r["repeated"]) for r in barge_rows(rig)] == [("resume", False)]


async def test_a_turn_that_cuts_jarvis_off_soon_after_a_resume_marks_it_a_likely_wrong_resume(
    rig_for: MakeRig,
) -> None:
    rig = await rig_for([*STORY, AwaitInterrupt()], [Text("Sure, sir.")])
    await speaking(rig)
    await resumed_over(rig, "")

    rig.audio.wake()
    await until(lambda: rig.state == "listening")
    rig.audio.say("Which is the second one?")
    await until(lambda: len(barge_rows(rig)) == 1)

    [row] = barge_rows(rig)
    assert (row["decision"], row["repeated"], row["next"]) == ("resume", True, "turn")


async def test_a_plain_follow_up_after_a_resume_is_no_repeat(
    rig_for: MakeRig,
) -> None:
    rig = await rig_for(STORY, [Text("Sure, sir.")], [Text("Fine, sir.")], recognition=FollowUpDecision())
    await speaking(rig)
    await resumed_over(rig, "Okay.")
    rig.player.finish()
    await rig.advance(0.1)
    rig.audio.say("Thanks, and what is the weather?")
    await until(lambda: len(barge_rows(rig)) == 1)
    assert [(r["repeated"], r["next"]) for r in barge_rows(rig)] == [(False, "turn")]


async def test_a_turn_long_after_the_resume_is_no_repeat(rig_for: MakeRig) -> None:
    rig = await rig_for(STORY, [Text("Sure, sir.")])
    await speaking(rig)
    await resumed_over(rig, "")
    rig.player.finish()
    await rig.advance(0.1)
    await rig.advance(9.0)

    rig.audio.wake()
    rig.audio.say("Wait, which is the second one?")
    await until(lambda: len(barge_rows(rig)) == 1)

    [row] = barge_rows(rig)
    assert (row["repeated"], row["next"]) == (False, None)


class LocalRelation:
    def __init__(self, verdict: Verdict) -> None:
        self.verdict: Verdict = verdict
        self.candidates: list[Any] = []

    async def assess(self, candidate: Any, *, current: Any, timeout_s: float = 2.0) -> Any:
        from jarvis_hub.voice.assessment import Assessment
        self.candidates.append(candidate)
        return Assessment(candidate.candidate_id, candidate.turn_id, self.verdict, 'scripted', 0)

    async def close(self) -> None:
        pass


async def test_resumed_thought_replaces_obsolete_generation_and_delegates_once(rig_for: MakeRig) -> None:
    relation = LocalRelation('amend')
    rig = await rig_for(
        [AwaitInterrupt(), Text('Obsolete reply.'), Tool('delegate', {'request': 'Open an issue'})],
        [ResolveScope('preserve_tasks'), Text('I will investigate the scheduler, sir.'), Tool('delegate', {'request': 'Open an issue about the scheduler'})],
        recognition=relation,
    )
    rig.audio.wake()
    rig.audio.say('Open an issue')
    await until(lambda: rig.state == 'thinking' and len(rig.user_messages()) == 1)
    rig.audio.say('about the scheduler')
    await until(lambda: len(rig.backend.all_sessions('orchestrator')) == 1)
    await rig.settle()

    assert rig.user_messages()[-1] == '<user lang="en">Open an issue about the scheduler</user>', rig.backend.session('talker').received
    assert len(rig.backend.session('orchestrator').received) == 1
    assert 'Obsolete reply.' not in rig.player.texts
    assert relation.candidates[0].admitted_turn == 'Open an issue'
    assert relation.candidates[0].phase == 'thinking'
    owners = [(row['request_id'], row['revision']) for row in rig.metrics.records if row['kind'] == 'user']
    assert owners[0][0] == owners[1][0]
    assert [revision for _, revision in owners] == [0, 1]


@pytest.mark.parametrize('verdict', ['unrelated', 'uncertain'])
async def test_unadmitted_thinking_speech_preserves_generation_and_audio(rig_for: MakeRig, verdict: Verdict) -> None:
    finish = asyncio.Event()
    relation = LocalRelation(verdict)
    rig = await rig_for([Gate(finish), Text('The original answer, sir.')], recognition=relation)
    rig.audio.wake()
    rig.audio.say('Explain the scheduler')
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say('Please pass the salt')
    await until(lambda: len(relation.candidates) == 1)
    await rig.settle()
    assert len(rig.user_messages()) == 1
    assert rig.state == 'thinking'
    finish.set()
    await until(lambda: rig.state == 'follow_up')
    assert 'The original answer, sir.' in rig.player.texts


class DelayedRelation(LocalRelation):
    def __init__(self) -> None:
        super().__init__('amend')
        self.release = asyncio.Event()

    async def assess(self, candidate: Any, *, current: Any, timeout_s: float = 2.0) -> Any:
        result = await super().assess(candidate, current=current, timeout_s=timeout_s)
        await self.release.wait()
        return result


@pytest.mark.parametrize('replacement', ['wake', 'typed'])
async def test_a_delayed_relation_cannot_revise_a_replaced_owner(rig_for: MakeRig, replacement: str) -> None:
    relation = DelayedRelation()
    rig = await rig_for([AwaitInterrupt()], [Text('Fresh answer, sir.')], recognition=relation)
    rig.audio.wake()
    rig.audio.say('Open an issue')
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say('about the old scheduler')
    await until(lambda: len(relation.candidates) == 1)
    if replacement == 'wake':
        rig.clock.advance(1)
        rig.audio.wake()
        await until(lambda: rig.state == 'listening')
        rig.audio.say('Check the current build')
    else:
        rig.typist.type('Check the current build')
    await until(lambda: len(rig.user_messages()) == 2)
    relation.release.set()
    await rig.settle()
    assert len(rig.user_messages()) == 2
    assert 'about the old scheduler' not in '\n'.join(rig.user_messages())


async def test_local_new_request_hint_preserves_the_opening_until_explicit_scope_choice(rig_for: MakeRig) -> None:
    relation = LocalRelation('new_request')
    rig = await rig_for([AwaitInterrupt()], [Text('Checking, sir.')], recognition=relation)
    rig.audio.wake()
    rig.audio.say('Explain the scheduler')
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say('Instead check the latest build')
    await until(lambda: len(rig.user_messages()) == 2)
    assert rig.user_messages()[-1] == '<user lang="en">Explain the scheduler Instead check the latest build</user>'


async def test_a_thinking_candidate_finishing_after_first_audio_revises_the_same_request(rig_for: MakeRig) -> None:
    relation = DelayedRelation()
    finish = asyncio.Event()
    rig = await rig_for([Gate(finish), Text('The old answer, sir. '), AwaitInterrupt()],
                        [Text('The combined answer, sir.')], recognition=relation)
    rig.audio.wake()
    await until(lambda: rig.player.voices[-1:] == ['chime'])
    rig.player.hold = True
    rig.audio.say('Explain the scheduler')
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say('including its retries')
    await until(lambda: len(relation.candidates) == 1)
    finish.set()
    await until(lambda: rig.state == 'speaking')
    relation.release.set()
    await until(lambda: len(rig.user_messages()) == 2)
    assert 'Explain the scheduler including its retries' in rig.user_messages()[-1]
    assert relation.candidates[0].phase == 'thinking'


async def test_a_revision_reuses_the_already_delegated_task_identity(rig_for: MakeRig) -> None:
    relation = LocalRelation('amend')
    rig = await rig_for(
        [Tool('delegate', {'request': 'Open an issue'}), AwaitInterrupt()],
        [ResolveScope('revise_tasks', ('T1',)), Tool('delegate', {'request': 'Open an issue about the scheduler', 'task_id': 'T1'}), Text('Updated, sir.')],
        orchestrator=[[], []], recognition=relation,
    )
    rig.audio.wake()
    rig.audio.say('Open an issue')
    await until(lambda: len(rig.backend.all_sessions('orchestrator')) == 1)
    await rig.settle()
    rig.audio.say('about the scheduler')
    await until(lambda: len(rig.backend.session('orchestrator').received) == 2)
    [first, second] = rig.backend.session('orchestrator').received
    assert '<task id="T1"' in first
    assert '<task id="T1"' in second
    assert 'T2' not in second


@pytest.mark.parametrize(('opening', 'fragment', 'lang'), [
    ('Open an issue', 'about the scheduler', 'en'),
    ('Apri una segnalazione', 'sul pianificatore', 'it'),
])
async def test_queued_fragments_are_assessed_together_before_revision_admission(
    rig_for: MakeRig, opening: str, fragment: str, lang: Any,
) -> None:
    relation = DelayedRelation()
    rig = await rig_for([AwaitInterrupt()], [AwaitInterrupt()], [Text('Done, sir.')], recognition=relation)
    rig.audio.wake()
    rig.audio.say(opening, lang)
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say(fragment, lang)
    await until(lambda: len(relation.candidates) == 1)
    rig.audio.say('today' if lang == 'en' else 'oggi', lang)
    relation.release.set()
    await until(lambda: len(relation.candidates) == 2)
    await until(lambda: len(rig.user_messages()) >= 2)
    await rig.settle()
    assert relation.candidates[1].admitted_turn == opening
    assert relation.candidates[1].transcript == f"{fragment} {'today' if lang == 'en' else 'oggi'}"
    assert len(rig.user_messages()) == 2
    assert f'{opening} {fragment}' in rig.user_messages()[-1]
    assert ('today' if lang == 'en' else 'oggi') in rig.user_messages()[-1]


async def test_oversized_continuation_is_reported_without_admission_or_interruption(rig_for: MakeRig) -> None:
    finish = asyncio.Event()
    relation = LocalRelation('amend')
    rig = await rig_for([Gate(finish), Text('The original answer, sir.')], recognition=relation)
    rig.audio.wake()
    rig.audio.say('Explain the scheduler')
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say('x' * 6000)
    await rig.settle()
    assert relation.candidates == []
    assert len(rig.user_messages()) == 1
    assert any('retention limit' in str(entry) for entry in rig.output.log)
    finish.set()
    await until(lambda: rig.state == 'follow_up')


async def test_a_duration_limited_fragment_is_reported_and_not_committed(rig_for: MakeRig) -> None:
    rig = await rig_for([Text('Must not run, sir.')])
    rig.audio.wake()
    rig.audio.say('An unfinished long request', limited=True)
    await rig.settle()
    assert rig.user_messages() == []
    assert any('duration limit' in str(entry) for entry in rig.output.log)


async def test_shutdown_discards_an_inflight_local_relation(rig_for: MakeRig) -> None:
    relation = DelayedRelation()
    rig = await rig_for([AwaitInterrupt()], recognition=relation)
    rig.audio.wake()
    rig.audio.say('Open an issue')
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say('about the old scheduler')
    await until(lambda: len(relation.candidates) == 1)
    await rig.voice.close()
    relation.release.set()
    await rig.settle()
    assert len(rig.user_messages()) == 1


async def test_new_request_selection_preserves_candidates_that_arrived_while_assessing(rig_for: MakeRig) -> None:
    relation = DelayedRelation()
    relation.verdict = 'new_request'
    rig = await rig_for([AwaitInterrupt()], [AwaitInterrupt()], [Text('Done, sir.')], recognition=relation)
    rig.audio.wake()
    rig.audio.say('Explain the scheduler')
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say('Check the build instead')
    await until(lambda: len(relation.candidates) == 1)
    rig.audio.say('Also check the tests')
    relation.release.set()
    await until(lambda: len(relation.candidates) == 2)
    await rig.settle()
    assert relation.candidates[1].transcript == 'Check the build instead Also check the tests'
    assert relation.candidates[1].admitted_turn == 'Explain the scheduler'


async def test_an_amended_multi_task_request_requires_explicit_task_ownership(rig_for: MakeRig) -> None:
    relation = LocalRelation('amend')
    rig = await rig_for(
        [Tool('delegate', {'request': 'Check build'}), Tool('delegate', {'request': 'Check docs'}), AwaitInterrupt()],
        [Tool('delegate', {'request': 'Check build and docs today'}), ResolveScope('revise_tasks', ('T1',)),
         Tool('delegate', {'request': 'Wrong task', 'task_id': 'T999'}),
         Tool('delegate', {'request': 'Conflicting intent', 'task_id': 'T1', 'new_task': True}),
         Tool('delegate', {'request': 'Check build today', 'task_id': 'T1'}),
         Tool('delegate', {'request': 'Also inspect release notes', 'new_task': True}), Text('Updated, sir.')],
        orchestrator=[[], [], [], []], recognition=relation,
    )
    rig.audio.wake()
    rig.audio.say('Check build and docs')
    await until(lambda: bool(rig.backend.all_sessions('orchestrator')) and len(rig.backend.session('orchestrator').received) == 2)
    rig.audio.say('today')
    await until(lambda: len(rig.backend.session('orchestrator').received) >= 4)
    await rig.settle()
    requests = rig.backend.session('orchestrator').received
    assert len(requests) == 4
    assert '<task id="T1"' in requests[-2]
    assert '<task id="T3"' in requests[-1]
    assert any('Choose task_id' in str(result.result) for result in rig.backend.session('talker').tool_results)


async def test_captured_thinking_onset_is_preserved_when_its_delivery_follows_first_audio(rig_for: MakeRig) -> None:
    relation = LocalRelation('amend')
    finish = asyncio.Event()
    rig = await rig_for([Gate(finish), Text('Old answer, sir. '), AwaitInterrupt()],
                        [Text('Revised answer, sir.')], recognition=relation)
    rig.audio.wake()
    await until(lambda: rig.player.voices[-1:] == ['chime'])
    rig.player.hold = True
    rig.audio.say('Explain the scheduler')
    await until(lambda: len(rig.user_messages()) == 1)
    onset, epoch = rig.clock.now(), rig.audio.owner
    rig.clock.advance(0.3)
    finish.set()
    await until(lambda: rig.state == 'speaking')
    rig.audio.emit(SpeechStarted(onset, epoch, 2))
    rig.audio.emit(Heard('including retries', 'en', onset + 0.1, epoch, 2, onset))
    await until(lambda: len(rig.user_messages()) == 2)
    assert relation.candidates[0].phase == 'thinking'
    assert 'Explain the scheduler including retries' in rig.user_messages()[-1]


async def test_duration_limit_cannot_admit_its_leftover_tail_as_a_fresh_request(rig_for: MakeRig) -> None:
    rig = await rig_for([Text('Must not run, sir.')])
    rig.audio.wake()
    rig.audio.say('An unfinished long request', limited=True)
    await rig.settle()
    rig.audio.say('its leftover tail')
    await rig.settle()
    assert rig.user_messages() == []


@pytest.mark.parametrize('report_late', [False, True])
async def test_a_revised_task_cannot_reuse_or_recreate_the_old_pending_approval(rig_for: MakeRig, report_late: bool) -> None:
    relation = LocalRelation('amend')
    report_gate = asyncio.Event()
    if not report_late:
        report_gate.set()
    approval: dict[str, Any] = {'action': 'subagent_action', 'description': 'Publish release 42', 'grants': []}
    rig = await rig_for(
        [Tool('delegate', {'request': 'Publish release 42'}), AwaitInterrupt()],
        [ResolveScope('revise_tasks', ('T1',)), Tool('delegate', {'request': 'Publish release 43 instead', 'task_id': 'T1'}), Text('Revised, sir.')],
        orchestrator=[[Gate(report_gate), Tool('report', {'task_id': 'T1', 'spoken': 'Publish release 42?',
                                                        'needs_user': True, 'approval': approval})], []],
        recognition=relation,
    )
    rig.audio.wake()
    rig.audio.say('Publish release 42')
    await until(lambda: bool(rig.backend.all_sessions('orchestrator')))
    await rig.settle()
    if not report_late:
        assert len(getattr(rig.hub, '_approvals').pending()) == 1
    rig.audio.say('release 43 instead')
    await until(lambda: len(rig.user_messages()) == 2)
    report_gate.set()
    await rig.settle()
    assert getattr(rig.hub, '_approvals').pending() == []
    assert len(rig.backend.session('orchestrator').received) == 2


async def test_actual_post_audio_onset_does_not_revise_the_thinking_owner(rig_for: MakeRig) -> None:
    relation = LocalRelation('amend')
    rig = await rig_for([Text('The original answer, sir. '), AwaitInterrupt()], recognition=relation)
    rig.audio.wake()
    await until(lambda: rig.player.voices[-1:] == ['chime'])
    rig.player.hold = True
    rig.audio.say('Explain the scheduler')
    await until(lambda: rig.state == 'speaking')
    rig.clock.advance(0.1)
    onset, epoch = rig.clock.now(), rig.audio.owner
    rig.audio.emit(SpeechStarted(onset + 0.02, epoch, 2, onset))
    rig.audio.emit(Heard('A separate overlap', 'en', onset + 0.2, epoch, 2, onset))
    await rig.settle()
    assert relation.candidates == []
    assert len(rig.user_messages()) == 1
    assert rig.state == 'speaking'


async def test_a_stale_producer_usage_limit_still_establishes_the_global_hold(rig_for: MakeRig) -> None:
    relation = LocalRelation('amend')
    report_gate = asyncio.Event()
    rig = await rig_for(
        [Tool('delegate', {'request': 'Check build'}), AwaitInterrupt()],
        [ResolveScope('revise_tasks', ('T1',)), Tool('delegate', {'request': 'Check build and tests', 'task_id': 'T1'}), Text('Updated, sir.')],
        orchestrator=[[Gate(report_gate), Limited(600)], []], recognition=relation,
    )
    rig.audio.wake()
    rig.audio.say('Check build')
    await until(lambda: bool(rig.backend.all_sessions('orchestrator')))
    rig.audio.say('and tests')
    await until(lambda: len(rig.user_messages()) == 2)
    report_gate.set()
    await until(lambda: any('Model unavailable: usage limit.' in notice for notice in rig.output.notices))
    await rig.settle()
    assert len(rig.backend.session('orchestrator').received) == 1


class PreparingWorkspace(FakeWorkspace):
    def __init__(self) -> None:
        super().__init__()
        self.began = asyncio.Event()
        self.release_prepare = asyncio.Event()

    async def scratch(self, clone: Path, name: str, holder: str) -> Path:
        self.began.set()
        await self.release_prepare.wait()
        return await super().scratch(clone, name, holder)


async def test_old_subagent_preparation_cannot_start_after_the_request_revision(rig_for: MakeRig) -> None:
    relation = LocalRelation('amend')
    workspace = PreparingWorkspace()
    spawn = {'repo': 'pumpkit', 'title': 'Old check', 'brief': 'Check the old target',
             'model': 'sonnet', 'effort': 'low', 'writes': False}
    rig = await rig_for(
        [Tool('delegate', {'request': 'Check build'}), AwaitInterrupt()],
        [ResolveScope('revise_tasks', ('T1',)), Tool('delegate', {'request': 'Check build and tests', 'task_id': 'T1'}), Text('Updated, sir.')],
        orchestrator=[[Tool('spawn_subagent', spawn)], []], recognition=relation, workspace=workspace,
    )
    rig.audio.wake()
    rig.audio.say('Check build')
    await workspace.began.wait()
    rig.audio.say('and tests')
    await until(lambda: len(rig.user_messages()) == 2)
    workspace.release_prepare.set()
    await until(lambda: len(rig.backend.session('orchestrator').received) == 2)
    await rig.settle()
    assert rig.backend.all_sessions('subagent') == []
    assert workspace.locks == {}


async def test_accepted_revision_intent_is_durable_before_the_new_talker_delegates(
    rig_for: MakeRig, tmp_path: Path,
) -> None:
    path = tmp_path / 'state.json'
    handoff = asyncio.Event()
    rig = await rig_for(
        [Tool('delegate', {'request': 'Publish release 42'}), AwaitInterrupt()],
        [Gate(handoff), ResolveScope('revise_tasks', ('T1',)), Tool('delegate', {'request': 'Publish release 43 instead', 'task_id': 'T1'})],
        orchestrator=[[AwaitInterrupt()]], recognition=LocalRelation('amend'), state_path=path,
    )
    rig.audio.wake()
    rig.audio.say('Publish release 42')
    await until(lambda: bool(rig.backend.all_sessions('orchestrator')))
    rig.audio.say('actually release 43')
    await until(lambda: len(rig.user_messages()) == 2)
    snapshot = json.loads(path.read_text())
    [task] = snapshot['tasks']
    assert task['revision'] == 0
    assert task['request'] == 'Publish release 42'
    [scope] = snapshot['spoken_scopes']
    assert scope['segments'] == ['Publish release 42', 'actually release 43']
    assert scope['pending'] is True
    restart_path = tmp_path / 'restart.json'
    restart_path.write_text(json.dumps(snapshot))
    restored = await rig_for([ResolveScope('revise_tasks', ('T1',)), Tool('delegate', {'request': 'Publish release 42 actually release 43', 'task_id': 'T1'})], orchestrator=[[AwaitInterrupt()]], state_path=restart_path)
    await until(lambda: bool(restored.backend.all_sessions('orchestrator')) and bool(restored.backend.session('orchestrator').received))
    assert 'Publish release 42 actually release 43' in restored.backend.session('orchestrator').received[0]
    handoff.set()


class HeldCleanupWorkspace(PreparingWorkspace):
    def __init__(self) -> None:
        super().__init__()
        self.cleaning = asyncio.Event()
        self.release_cleanup = asyncio.Event()

    async def discard(self, worktree: Path, holder: str) -> str | None:
        self.cleaning.set()
        await self.release_cleanup.wait()
        return await super().discard(worktree, holder)


async def test_restart_cannot_run_an_obsolete_prepared_subagent_during_cleanup(
    rig_for: MakeRig, tmp_path: Path,
) -> None:
    path = tmp_path / 'state.json'
    workspace = HeldCleanupWorkspace()
    spawn = {'repo': 'pumpkit', 'title': 'Old check', 'brief': 'Check the old target',
             'model': 'sonnet', 'effort': 'low', 'writes': False}
    rig = await rig_for(
        [Tool('delegate', {'request': 'Check build'}), AwaitInterrupt()],
        [ResolveScope('revise_tasks', ('T1',)), Tool('delegate', {'request': 'Check build and tests', 'task_id': 'T1'}), Text('Updated, sir.')],
        orchestrator=[[Tool('spawn_subagent', spawn)], [AwaitInterrupt()]], recognition=LocalRelation('amend'),
        workspace=workspace, state_path=path,
    )
    rig.audio.wake()
    rig.audio.say('Check build')
    await workspace.began.wait()
    rig.audio.say('and tests')
    await until(lambda: len(rig.user_messages()) == 2)
    workspace.release_prepare.set()
    await workspace.cleaning.wait()
    await rig.settle()
    snapshot = json.loads(path.read_text())
    [child] = snapshot['subagents']
    assert child['task_revision'] == 0
    assert snapshot['tasks'][0]['revision'] == 1
    restart_path = tmp_path / 'restart.json'
    restart_path.write_text(json.dumps(snapshot))
    workspace.release_cleanup.set()
    restored = await rig_for(orchestrator=[[AwaitInterrupt()]], state_path=restart_path)
    await until(lambda: bool(restored.backend.all_sessions('orchestrator')))
    await restored.settle()
    assert restored.backend.all_sessions('subagent') == []
    assert 'Old check' not in restored.backend.session('orchestrator').received[0]
    workspace.release_cleanup.set()


@pytest.mark.parametrize('phase', ['thinking', 'speaking'])
async def test_local_stop_reply_silences_the_generation_without_admitting_a_new_request(rig_for: MakeRig, phase: str) -> None:
    relation = DelayedRelation()
    relation.verdict = 'stop_reply'
    speaking_gate = asyncio.Event()
    rig = await rig_for([Gate(speaking_gate), Text('Old answer, sir. '), AwaitInterrupt()], recognition=relation)
    rig.audio.wake()
    await until(lambda: rig.player.voices[-1:] == ['chime'])
    rig.player.hold = True
    rig.audio.say('Explain the scheduler')
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say('Please stop replying')
    await until(lambda: len(relation.candidates) == 1)
    if phase == 'speaking':
        speaking_gate.set()
        await until(lambda: rig.state == 'speaking')
    relation.release.set()
    await until(lambda: rig.state == 'listening')
    await rig.settle()
    assert len(rig.user_messages()) == 1
    assert rig.player.pending() == 0
    speaking_gate.set()


async def test_stop_reply_preserves_the_already_delegated_background_task(rig_for: MakeRig) -> None:
    finished = asyncio.Event()
    rig = await rig_for(
        [Tool('delegate', {'request': 'Check build'}), AwaitInterrupt()],
        orchestrator=[[Gate(finished), Tool('report', {'task_id': 'T1', 'spoken': 'The build passed.', 'needs_user': False})]],
        recognition=LocalRelation('stop_reply'),
    )
    rig.audio.wake()
    rig.audio.say('Check build')
    await until(lambda: bool(rig.backend.all_sessions('orchestrator')))
    rig.audio.say('Please stop replying')
    await until(lambda: rig.state == 'listening')
    finished.set()
    await until(lambda: bool(rig.backend.session('orchestrator').tool_results))
    [reported] = rig.backend.session('orchestrator').tool_results
    assert reported.result == 'Reported to the User.'
    assert len(rig.user_messages()) == 1


async def test_amending_one_task_during_shared_recovery_preserves_the_other_task(
    rig_for: MakeRig, tmp_path: Path,
) -> None:
    path = tmp_path / 'state.json'
    seed = await rig_for(
        [Tool('delegate', {'request': 'Check build'}), Tool('delegate', {'request': 'Check docs'}), Text('Checking, sir.')],
        orchestrator=[[AwaitInterrupt()], []], state_path=path,
    )
    seed.audio.wake()
    seed.audio.say('Check build and docs')
    await until(lambda: len(seed.backend.session('talker').tool_results) >= 2)
    restart_path = tmp_path / 'restart.json'
    restart_path.write_text(path.read_text())
    report_gate = asyncio.Event()
    rig = await rig_for(
        [Tool('delegate', {'request': 'Check build', 'task_id': 'T1'}), AwaitInterrupt()],
        [ResolveScope('revise_tasks', ('T1',)), Tool('delegate', {'request': 'Check build and tests', 'task_id': 'T1'}), Text('Updated, sir.')],
        orchestrator=[[
            Gate(report_gate),
            Tool('report', {'task_id': 'T1', 'spoken': 'Old build result', 'needs_user': False}),
            Tool('report', {'task_id': 'T2', 'spoken': 'Docs are checked', 'needs_user': False}),
        ], []], state_path=restart_path, recognition=LocalRelation('amend'),
    )
    await until(lambda: bool(rig.backend.all_sessions('orchestrator')))
    rig.audio.wake()
    rig.audio.say('Check build')
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say('and tests')
    await until(lambda: len(rig.user_messages()) == 2)
    report_gate.set()
    await until(lambda: len(rig.backend.session('orchestrator').tool_results) >= 2)
    [old, unrelated] = rig.backend.session('orchestrator').tool_results[:2]
    assert old.result == 'Refused: this Task revision was superseded.'
    assert unrelated.result == 'Reported to the User.'
    status = next(tool.handler for tool in rig.backend.session('talker').spec.tools if tool.name == 'status')
    assert 'T2 done' in await status({})


async def test_no_saved_revision_snapshot_keeps_the_old_live_approval(
    rig_for: MakeRig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jarvis_hub.state import StateFile
    snapshots: list[dict[str, Any]] = []
    save = StateFile.save
    def capture(file: StateFile, data: dict[str, Any]) -> None:
        snapshots.append(json.loads(json.dumps(data)))
        save(file, data)
    monkeypatch.setattr(StateFile, 'save', capture)
    handoff = asyncio.Event()
    approval: dict[str, Any] = {'action': 'subagent_action', 'description': 'Publish release 42', 'grants': []}
    rig = await rig_for(
        [Tool('delegate', {'request': 'Publish release 42'}), AwaitInterrupt()],
        [Gate(handoff), ResolveScope('revise_tasks', ('T1',)), Text('Revised, sir.')],
        orchestrator=[[Tool('report', {'task_id': 'T1', 'spoken': 'Publish release 42?',
                                      'needs_user': True, 'approval': approval})]],
        recognition=LocalRelation('amend'), state_path=tmp_path / 'state.json',
    )
    rig.audio.wake()
    rig.audio.say('Publish release 42')
    await until(lambda: any(snapshot['approvals'] for snapshot in snapshots))
    rig.audio.say('actually release 43')
    await until(lambda: len(rig.user_messages()) == 2)
    handoff.set()
    await until(lambda: any(snapshot['tasks'] and snapshot['tasks'][0]['revision'] == 1 for snapshot in snapshots))
    revised = [snapshot for snapshot in snapshots if snapshot['tasks'] and snapshot['tasks'][0]['revision'] == 1]
    assert revised
    assert all('actually release 43' in snapshot['tasks'][0]['request'] for snapshot in revised)
    assert all(not snapshot['approvals'] for snapshot in revised)
    handoff.set()


async def test_an_amended_turn_can_first_bind_an_explicit_existing_task(rig_for: MakeRig) -> None:
    finished = asyncio.Event()
    rig = await rig_for(
        [Tool('delegate', {'request': 'Check build'}), Text('Checking, sir.')],
        [AwaitInterrupt()],
        [ResolveScope('preserve_tasks'), Tool('delegate', {'request': 'Continue the build check including tests', 'task_id': 'T1'}), Text('Updated, sir.')],
        orchestrator=[[Gate(finished), Tool('report', {'task_id': 'T1', 'spoken': 'Build checked', 'needs_user': False})], []], recognition=LocalRelation('amend'),
    )
    rig.typist.type('Check build')
    await until(lambda: bool(rig.backend.all_sessions('orchestrator')))
    await rig.settle()
    rig.audio.wake()
    rig.audio.say('Continue the build check')
    await until(lambda: len(rig.user_messages()) == 2)
    rig.audio.say('including tests')
    await until(lambda: len(rig.user_messages()) == 3)
    finished.set()
    await rig.settle()
    requests = rig.backend.session('orchestrator').received
    assert len(requests) == 2
    assert '<task id="T1"' in requests[-1]


class SequenceRelation(LocalRelation):
    def __init__(self, verdicts: list[Verdict], *, hold_first: bool = False) -> None:
        super().__init__('uncertain')
        self.verdicts = iter(verdicts)
        self.first = asyncio.Event()
        if not hold_first:
            self.first.set()

    async def assess(self, candidate: Any, *, current: Any, timeout_s: float = 2.0) -> Any:
        self.verdict = next(self.verdicts)
        result = await super().assess(candidate, current=current, timeout_s=timeout_s)
        if len(self.candidates) == 1:
            await self.first.wait()
        return result


async def test_uncertain_fragment_is_reassessed_and_admitted_with_its_continuation(rig_for: MakeRig) -> None:
    relation = SequenceRelation(['uncertain', 'amend'])
    rig = await rig_for([AwaitInterrupt()], [Text('Updated, sir.')], recognition=relation)
    rig.audio.wake()
    rig.audio.say('Open an issue')
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say('about the worker')
    await until(lambda: len(relation.candidates) == 1)
    await rig.settle()
    rig.audio.say('that drops callbacks')
    await until(lambda: len(rig.user_messages()) == 2)
    assert relation.candidates[-1].transcript == 'about the worker that drops callbacks'
    assert 'Open an issue about the worker that drops callbacks' in rig.user_messages()[-1]


async def test_newer_queued_speech_prevents_admission_of_an_obsolete_partial_candidate(rig_for: MakeRig) -> None:
    relation = SequenceRelation(['amend', 'amend'], hold_first=True)
    rig = await rig_for([AwaitInterrupt()], [Text('Updated, sir.')], recognition=relation)
    rig.audio.wake()
    rig.audio.say('Open an issue')
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say('about the worker')
    await until(lambda: len(relation.candidates) == 1)
    rig.audio.say('that drops callbacks')
    await rig.settle()
    relation.first.set()
    await until(lambda: len(relation.candidates) == 2)
    await until(lambda: len(rig.user_messages()) == 2)
    await rig.settle()
    assert relation.candidates[-1].transcript == 'about the worker that drops callbacks'
    assert len(rig.user_messages()) == 2
    assert 'Open an issue about the worker that drops callbacks' in rig.user_messages()[-1]


async def test_confirmed_unrelated_fragments_do_not_exhaust_continuation_retention(rig_for: MakeRig) -> None:
    verdicts: list[Verdict] = ['unrelated'] * 6
    verdicts.append('amend')
    relation = SequenceRelation(verdicts)
    rig = await rig_for([AwaitInterrupt()], [Text('Updated, sir.')], recognition=relation)
    rig.audio.wake()
    rig.audio.say('Open an issue')
    await until(lambda: len(rig.user_messages()) == 1)
    for index in range(6):
        rig.audio.say(f'Unrelated nearby dialogue {index}')
        await until(lambda: len(relation.candidates) == index + 1)
        await rig.settle()
    rig.audio.say('about the scheduler')
    await until(lambda: len(rig.user_messages()) == 2)
    assert relation.candidates[-1].transcript == 'about the scheduler'
    assert 'Open an issue about the scheduler' in rig.user_messages()[-1]


async def test_newer_onset_waits_for_its_transcript_before_reassessing_the_preserved_prefix(rig_for: MakeRig) -> None:
    relation = SequenceRelation(['amend', 'amend'], hold_first=True)
    rig = await rig_for([AwaitInterrupt()], [Text('Updated, sir.')], recognition=relation)
    rig.audio.wake()
    rig.audio.say('Open an issue')
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say('about the worker')
    await until(lambda: len(relation.candidates) == 1)
    rig.audio.speech()
    await rig.settle()
    relation.first.set()
    await rig.settle()
    assert len(relation.candidates) == 1
    assert len(rig.user_messages()) == 1
    rig.audio.emit(Heard('that drops callbacks', 'en', rig.clock.now(), None, rig.audio.candidate, rig.clock.now()))
    await until(lambda: len(rig.user_messages()) == 2)
    assert relation.candidates[-1].transcript == 'about the worker that drops callbacks'
    assert 'Open an issue about the worker that drops callbacks' in rig.user_messages()[-1]


async def test_retention_limit_counts_the_full_uncertain_prefix_and_new_fragment(rig_for: MakeRig) -> None:
    finished = asyncio.Event()
    relation = SequenceRelation(['uncertain'])
    rig = await rig_for([Gate(finished), Text('The original answer, sir.')], recognition=relation)
    rig.audio.wake()
    rig.audio.say('Explain the scheduler')
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say('x' * 2900)
    await until(lambda: len(relation.candidates) == 1)
    await rig.settle()
    rig.audio.say('y' * 3200)
    await rig.settle()
    assert len(relation.candidates) == 1
    assert len(rig.user_messages()) == 1
    assert any('retention limit' in notice for notice in rig.output.notices)
    finished.set()
    await until(lambda: rig.state == 'follow_up')


async def test_coalesced_uncertainty_keeps_earliest_onset_and_current_played_context(rig_for: MakeRig) -> None:
    reply = asyncio.Event()
    relation = SequenceRelation(['uncertain', 'amend'])
    rig = await rig_for([Gate(reply), Text('The old answer, sir. '), AwaitInterrupt()],
                        [Text('Updated, sir.')], recognition=relation)
    rig.audio.wake()
    rig.audio.say('Open an issue')
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say('about the worker')
    await until(lambda: len(relation.candidates) == 1)
    await rig.settle()
    reply.set()
    await until(lambda: rig.state == 'speaking')
    rig.clock.advance(0.1)
    rig.audio.say('that drops callbacks')
    await until(lambda: len(relation.candidates) == 2)
    await rig.settle()
    candidate = relation.candidates[-1]
    assert candidate.transcript == 'about the worker that drops callbacks'
    assert candidate.phase == 'thinking'
    assert candidate.started_before_reply is True
    assert 'The old answer, sir.' in candidate.played_text
    assert any('Open an issue about the worker that drops callbacks' in message
               for message in rig.backend.session('talker').received)


@pytest.mark.parametrize("ending", ["That's all.", "Basta così."])
async def test_a_whole_end_control_after_wake_adds_no_speech_or_model_turn(rig_for: MakeRig, ending: str) -> None:
    rig = await rig_for()
    turns = len(talker_received(rig))
    rig.audio.wake()
    rig.audio.say(ending)
    await until(lambda: rig.state == "idle")
    await rig.advance(60)

    assert rig.user_messages() == []
    assert rig.player.voices == ["chime"]
    assert len(talker_received(rig)) == turns


@pytest.mark.parametrize(
    ("utterance", "lang"),
    [("Thanks Jarvis, check the CI", "en"), ("Grazie Jarvis, controlla le PR", "it")],
)
async def test_longer_spoken_requests_keep_their_thanks_and_reach_the_talker(
    rig_for: MakeRig, utterance: str, lang: str
) -> None:
    rig = await rig_for([Text("The checks passed.")])
    rig.audio.wake()
    rig.audio.say(utterance)
    await until(lambda: rig.state == "follow_up")

    assert rig.user_messages() == [f'<user lang="{lang}">{utterance}</user>']
    assert rig.player.texts == ["", "The checks passed."]


@pytest.mark.parametrize("ending", ["That's all.", "Grazie Jarvis."])
async def test_farewell_words_cannot_take_the_floor_through_a_non_capturing_transcript(
    rig_for: MakeRig, ending: str
) -> None:
    release = asyncio.Event()
    rig = await rig_for([Text("The checks are running. "), Gate(release), Text("All green.")])
    rig.audio.wake()
    rig.audio.say("Check the CI")
    await until(lambda: rig.state == "speaking" and "The checks are running." in rig.player.texts)
    turns = len(talker_received(rig))
    events = rig.player.events.copy()
    try:
        rig.audio.say(ending)
        await rig.settle()
        assert rig.state == "speaking"
        assert rig.player.events == events
        assert len(talker_received(rig)) == turns
    finally:
        release.set()
    await until(lambda: rig.state == "follow_up")
    assert rig.player.texts == ["", "The checks are running.", "All green."]


@pytest.mark.parametrize(
    ("ending", "response"),
    [("That's all.", "Until tomorrow, sir."), ("Basta così.", "A domani, signore.")],
)
async def test_typed_end_controls_remain_generated_text_without_audio(rig_for: MakeRig, ending: str, response: str) -> None:
    rig = await rig_for([Text(response), Tool("end_conversation")])
    rig.typist.type(ending)
    await until(lambda: rig.output.replies == [response])
    turns = len(talker_received(rig))
    await rig.advance(60)

    assert len(rig.user_messages()) == 1
    assert 'typed="true"' in rig.user_messages()[0]
    assert rig.player.texts == []
    assert rig.state == "idle"
    assert len(talker_received(rig)) == turns


@pytest.mark.parametrize('pending', [True, False])
async def test_capture_abort_discards_unadmitted_fragments_without_stopping_owned_work(
    rig_for: MakeRig, pending: bool,
) -> None:
    from jarvis_hub.voice.state import CaptureAborted
    class AbortedRelation(LocalRelation):
        def __init__(self) -> None:
            super().__init__('amend' if pending else 'uncertain')
            self.release = asyncio.Event()
        async def assess(self, candidate: Any, *, current: Any, timeout_s: float = 2.0) -> Any:
            result = await super().assess(candidate, current=current, timeout_s=timeout_s)
            if pending and len(self.candidates) == 1:
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    await self.release.wait()
            return result
    relation, finish = AbortedRelation(), asyncio.Event()
    rig = await rig_for(
        [Tool('delegate', {'request': 'Check build'}), Gate(finish), Text('The original answer, sir.')],
        [Text('An obsolete fragment, sir.')], orchestrator=[[AwaitInterrupt()]], recognition=relation,
    )
    rig.audio.wake()
    rig.audio.say('Check build')
    await until(lambda: bool(rig.backend.all_sessions('orchestrator')))
    rig.audio.say('about the obsolete fragment')
    await until(lambda: len(relation.candidates) == 1)
    await rig.settle()
    rig.audio.speech()
    rig.audio.emit(CaptureAborted(rig.clock.now(), 'overflow', rig.audio.owner, rig.audio.candidate))
    await rig.settle()
    relation.release.set()
    await rig.settle()
    assert len(rig.user_messages()) == 1
    assert rig.state == 'thinking'
    status = next(tool.handler for tool in rig.backend.session('talker').spec.tools if tool.name == 'status')
    assert 'Check build' in await status({})
    relation.verdict = 'uncertain'
    rig.audio.say('a fresh fragment')
    await until(lambda: len(relation.candidates) == 2)
    assert relation.candidates[-1].transcript == 'a fresh fragment'
    finish.set()
    await until(lambda: rig.state == 'follow_up')
    assert 'The original answer, sir.' in rig.player.texts


async def test_an_old_capture_abort_cannot_discard_a_replacement_owners_assessment(rig_for: MakeRig) -> None:
    from jarvis_hub.voice.state import CaptureAborted
    relation = DelayedRelation()
    rig = await rig_for([AwaitInterrupt()], [AwaitInterrupt()],
                        [ResolveScope('preserve_tasks'), Text('Updated, sir.')], recognition=relation)
    rig.audio.wake()
    rig.audio.say('The old request')
    await until(lambda: len(rig.user_messages()) == 1)
    old_owner = rig.audio.owner
    await rig.advance(.6)
    rig.audio.wake()
    await until(lambda: rig.state == 'listening')
    rig.audio.say('The fresh request')
    await until(lambda: len(rig.user_messages()) == 2)
    rig.audio.say('with its continuation')
    await until(lambda: len(relation.candidates) == 1)
    assert rig.audio.owner != old_owner
    rig.audio.emit(CaptureAborted(rig.clock.now(), 'overflow', old_owner, rig.audio.candidate))
    await rig.settle()
    relation.release.set()
    await until(lambda: len(rig.user_messages()) == 3)
    assert 'The fresh request with its continuation' in rig.user_messages()[-1]
