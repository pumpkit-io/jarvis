"""Follow-up speech needs contextual admission within its original source-time window."""

import asyncio
import json
from pathlib import Path
from collections.abc import Callable

import pytest

from jarvis_hub.voice.assessment import Assessment, Candidate, SegmentAction
from jarvis_hub.voice.state import Heard, SpeechStarted
from tests.fakes import Text, until
from tests.test_voice_mode import MakeRig, rig_for as rig_for


class Decisions:
    def __init__(self, actions: list[tuple[SegmentAction, ...]], gate: asyncio.Event | None = None) -> None:
        self.actions = iter(actions)
        self.gate = gate
        self.candidates: list[Candidate] = []

    async def assess(self, candidate: Candidate, *, current: Callable[[], tuple[str, str]], timeout_s: float = 2.0) -> Assessment:
        self.candidates.append(candidate)
        actions = next(self.actions)
        if self.gate is not None:
            await self.gate.wait()
        return Assessment(candidate.candidate_id, candidate.turn_id, "directed", "scripted", 0, actions)

    async def close(self) -> None:
        pass


async def test_disabled_follow_up_never_sends_a_background_request_or_extends_the_window(rig_for: MakeRig) -> None:
    rig = await rig_for([Text("The answer, sir.")], [Text("Must not run.")])
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(5)
    rig.audio.say("Lena, carry on with dinner.")
    await rig.settle()
    assert len(rig.user_messages()) == 1
    await rig.advance(1)
    assert rig.state == "idle"


@pytest.mark.parametrize(("speech", "action"), [("Lena, close the door.", "ignore"), ("Mhm.", "ignore"), ("Luca, chiudi la porta.", "ignore"), ("Forse dopo.", "uncertain")])
async def test_rejected_speech_keeps_the_original_expiry(rig_for: MakeRig, speech: str, action: SegmentAction) -> None:
    decisions = Decisions([(action,)])
    rig = await rig_for([Text("It is green, sir.")], [Text("Must not run.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Is the build green?")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(5)
    rig.audio.say(speech)
    await until(lambda: len(decisions.candidates) == 1)
    await rig.settle()
    assert len(rig.user_messages()) == 1
    await rig.advance(1)
    assert rig.state == "idle"


@pytest.mark.parametrize(("opening", "reply", "follow_up"), [("Show the status", "The build is green, sir.", "And what about the tests?"), ("Mostra lo stato", "La build è verde, signore.", "E i test?")])
async def test_owned_follow_up_preserves_raw_opening_and_exact_new_source(rig_for: MakeRig, opening: str, reply: str, follow_up: str) -> None:
    decisions = Decisions([("floor",)])
    rig = await rig_for([Text(reply)], [Text("The tests pass.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say(opening)
    await until(lambda: rig.state == "follow_up")
    rig.audio.say(follow_up)
    await until(lambda: len(rig.user_messages()) == 2)
    assert follow_up in rig.user_messages()[-1] and opening in rig.user_messages()[-1]
    assert decisions.candidates[0].phase == "follow_up"
    assert opening in decisions.candidates[0].admitted_turn
    assert reply in decisions.candidates[0].played_text


async def test_predeadline_source_onset_can_complete_and_classify_after_expiry(rig_for: MakeRig) -> None:
    decisions = Decisions([("floor",)])
    rig = await rig_for([Text("The build is green.")], [Text("The tests pass.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(5.9)
    onset = rig.clock.now()
    candidate = rig.audio.candidate + 1
    rig.audio.emit(SpeechStarted(onset, rig.audio.owner, candidate, onset))
    await rig.advance(1)
    rig.audio.emit(Heard("And the tests?", None, rig.clock.now(), rig.audio.owner, candidate, onset))
    await until(lambda: len(rig.user_messages()) == 2)
    assert "And the tests?" in rig.user_messages()[-1]


async def test_afterdeadline_onset_cannot_join_an_earlier_uncertain_fragment(rig_for: MakeRig) -> None:
    decisions = Decisions([("uncertain",), ("floor", "floor")])
    rig = await rig_for([Text("The build is green.")], [Text("Must not run.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(5.9)
    rig.audio.say("And perhaps")
    await until(lambda: len(decisions.candidates) == 1)
    await rig.advance(.2)
    rig.audio.say("Jarvis, the tests?")
    await rig.settle()
    assert len(decisions.candidates) == 1 and len(rig.user_messages()) == 1
    assert rig.state == "idle"


async def test_follow_up_assessment_is_stale_after_typed_replacement(rig_for: MakeRig) -> None:
    gate = asyncio.Event()
    decisions = Decisions([("floor",)], gate)
    rig = await rig_for([Text("The build is green.")], [Text("Typed status.")], [Text("Must not run.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "follow_up")
    rig.audio.say("And the tests?")
    await until(lambda: len(decisions.candidates) == 1)
    rig.typist.type("Show the Task list")
    await until(lambda: "Typed status." in rig.output.replies)
    gate.set()
    await rig.settle()
    assert len(rig.user_messages()) == 2
    assert rig.player.texts[-1] != "Typed status."


async def test_delayed_onset_delivery_keeps_source_time_eligibility_after_idle(rig_for: MakeRig) -> None:
    decisions = Decisions([("floor",)])
    rig = await rig_for([Text("The build is green.")], [Text("The tests pass.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(5.9)
    onset, owner = rig.clock.now(), rig.audio.owner
    candidate = rig.audio.candidate + 1
    await rig.advance(.2)
    assert rig.state == "idle"
    rig.audio.emit(SpeechStarted(rig.clock.now(), owner, candidate, onset))
    rig.audio.emit(Heard("And the tests?", None, rig.clock.now(), owner, candidate, onset))
    await until(lambda: len(rig.user_messages()) == 2)


async def test_raw_wake_hit_inside_a_follow_up_does_not_bypass_contextual_rejection(rig_for: MakeRig) -> None:
    decisions = Decisions([("ignore",)])
    rig = await rig_for([Text("The build is green.")], [Text("Must not run.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "follow_up")
    chimes = rig.player.voices.count("chime")
    await rig.advance(5)
    rig.audio.wake()
    rig.audio.say("Lena, the note says Jarvis stop.")
    await until(lambda: len(decisions.candidates) == 1)
    await rig.advance(1)
    assert rig.state == "idle" and len(rig.user_messages()) == 1
    assert rig.player.voices.count("chime") == chimes


async def test_postexpiry_speech_cannot_piggyback_an_eligible_inflight_decision(rig_for: MakeRig) -> None:
    gate = asyncio.Event()
    decisions = Decisions([("floor",)], gate)
    rig = await rig_for([Text("The build is green.")], [Text("The tests pass.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(5.9)
    rig.audio.say("And the tests?")
    await until(lambda: len(decisions.candidates) == 1)
    await rig.advance(.2)
    rig.audio.say("And deploy it now")
    gate.set()
    await until(lambda: len(rig.user_messages()) == 2)
    assert "And the tests?" in rig.user_messages()[-1]
    assert "deploy" not in rig.user_messages()[-1]
    assert len(decisions.candidates) == 1


async def test_abandoned_capture_discards_pending_source_without_extending_expiry(rig_for: MakeRig) -> None:
    from jarvis_hub.voice.state import CaptureAborted

    gate = asyncio.Event()
    decisions = Decisions([("floor",)], gate)
    rig = await rig_for([Text("The build is green.")], [Text("Must not run.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(5)
    rig.audio.say("And the tests?")
    await until(lambda: len(decisions.candidates) == 1)
    rig.audio.emit(CaptureAborted(rig.clock.now(), "overflow", rig.audio.owner, rig.audio.candidate))
    await rig.settle()
    gate.set()
    await rig.advance(1)
    assert rig.state == "idle" and len(rig.user_messages()) == 1


async def test_uncertain_background_is_not_selected_with_a_later_directed_follow_up(rig_for: MakeRig) -> None:
    decisions = Decisions([("uncertain",), ("ignore", "floor")])
    rig = await rig_for([Text("The build is green.")], [Text("The tests pass.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "follow_up")
    rig.audio.say("Maybe after dinner")
    await until(lambda: len(decisions.candidates) == 1)
    await rig.settle()
    rig.audio.say("Jarvis, what about the tests?")
    await until(lambda: len(rig.user_messages()) == 2)
    assert "dinner" not in rig.user_messages()[-1]
    assert "Jarvis, what about the tests?" in rig.user_messages()[-1]


async def test_announcement_follow_up_uses_announcement_context_and_no_unrelated_request(rig_for: MakeRig) -> None:
    from tests.test_voice_mode import NEWS, arrive, news_rig

    decisions = Decisions([("floor",)])
    rig, [arrival] = await news_rig(rig_for, [Text(NEWS)], [Text("It was pumpkit.")], recognition=decisions)
    await arrive(rig, arrival)
    await until(lambda: NEWS in rig.player.texts and rig.state == "follow_up")
    rig.audio.say("Which repository was that?")
    await until(lambda: len(decisions.candidates) == 1 and len(rig.user_messages()) == 2)
    assert decisions.candidates[0].admitted_turn == ""
    assert NEWS in decisions.candidates[0].played_text
    assert "Which repository was that?" in rig.user_messages()[-1]


async def test_stop_only_follow_up_closes_without_cancelling_a_background_task(rig_for: MakeRig) -> None:
    from tests.test_voice_mode import news_rig

    decisions = Decisions([("stop",)])
    rig, _ = await news_rig(rig_for, [Text("The build is green.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "follow_up")
    status = next(tool.handler for tool in rig.backend.session("talker").spec.tools if tool.name == "status")
    before = await status({})
    rig.audio.say("Jarvis, stop answering for now")
    await until(lambda: rig.state == "idle")
    assert await status({}) == before and "cancelled" not in before
    assert len(rig.user_messages()) == 2


async def test_afterdeadline_abort_cannot_invalidate_an_earlier_eligible_candidate(rig_for: MakeRig) -> None:
    from jarvis_hub.voice.state import CaptureAborted

    gate = asyncio.Event()
    decisions = Decisions([("floor",)], gate)
    rig = await rig_for([Text("The build is green.")], [Text("The tests pass.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(5.9)
    rig.audio.say("And the tests?")
    await until(lambda: len(decisions.candidates) == 1)
    await rig.advance(.2)
    rig.audio.speech()
    rig.audio.emit(CaptureAborted(rig.clock.now(), "overflow", rig.audio.owner, rig.audio.candidate))
    await rig.settle()
    gate.set()
    await until(lambda: len(rig.user_messages()) == 2)
    assert "And the tests?" in rig.user_messages()[-1]


@pytest.mark.parametrize("disposition", ["preserve_tasks", "new_request"])
async def test_owned_follow_up_holds_old_task_authority_until_scope_resolution(rig_for: MakeRig, disposition: str) -> None:
    import json
    from tests.fakes import Gate, ResolveScope, Tool

    report_gate, resolve_gate = asyncio.Event(), asyncio.Event()
    decisions = Decisions([("floor",)])
    rig = await rig_for(
        [Text("Checking the build."), Tool("delegate", {"request": "Check the build"})],
        [Gate(resolve_gate), ResolveScope(disposition, from_segment=1 if disposition == "new_request" else None), Text("Understood.")],
        orchestrator=[[Gate(report_gate), Tool("report", {"task_id": "T1", "spoken": "The build is green.", "needs_user": False})]],
        recognition=decisions,
    )
    rig.audio.wake()
    rig.audio.say("Check the build")
    await until(lambda: rig.state == "follow_up" and bool(rig.backend.all_sessions("orchestrator")))
    rig.audio.say("And tell me the time instead")
    await until(lambda: len(rig.user_messages()) == 2)
    report_gate.set()
    await rig.settle()
    orchestrator = rig.backend.session("orchestrator")
    assert not any(result.name == "report" for result in orchestrator.tool_results)
    status = next(tool.handler for tool in rig.backend.session("talker").spec.tools if tool.name == "status")
    assert "Check the build" in await status({})
    resolve_gate.set()
    await until(lambda: any(result.name == "report" for result in orchestrator.tool_results))
    assert "Check the build" in await status({})
    assert not any(json.loads(result.result or "{}").get("task_id") == "T2" for result in rig.backend.session("talker").tool_results if result.name == "delegate")


async def test_rejected_thinking_origin_delivered_in_follow_up_keeps_window_expiry(rig_for: MakeRig) -> None:
    from tests.fakes import Gate

    answer = asyncio.Event()
    decisions = Decisions([("uncertain",)])
    rig = await rig_for([Gate(answer), Text("The build is green.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "thinking")
    await rig.advance(.1)
    onset, owner = rig.clock.now(), rig.audio.owner
    candidate = rig.audio.candidate + 1
    await rig.advance(.1)
    answer.set()
    await until(lambda: rig.state == "follow_up")
    rig.audio.emit(SpeechStarted(rig.clock.now(), owner, candidate, onset))
    rig.audio.emit(Heard("Perhaps", None, rig.clock.now(), owner, candidate, onset))
    await until(lambda: len(decisions.candidates) == 1)
    await rig.advance(6)
    assert rig.state == "idle" and len(rig.user_messages()) == 1


async def test_follow_up_cannot_split_an_earlier_thinking_origin_assessment(rig_for: MakeRig) -> None:
    from tests.fakes import Gate, ResolveScope

    answer, classification = asyncio.Event(), asyncio.Event()
    decisions = Decisions([("uncertain",), ("floor", "floor")], classification)
    rig = await rig_for([Gate(answer), Text("The build is green.")], [ResolveScope("preserve_tasks"), Text("The tests pass.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "thinking")
    rig.audio.say("And compare")
    await until(lambda: len(decisions.candidates) == 1)
    answer.set()
    await until(lambda: rig.state == "follow_up")
    rig.audio.say("the test results")
    await rig.settle()
    assert len(decisions.candidates) == 1
    classification.set()
    await until(lambda: len(decisions.candidates) == 2)
    await until(lambda: ("said", "Show the status And compare the test results") in rig.output.log)
    assert decisions.candidates[-1].observed_segments == ("And compare", "the test results")
    assert sum(text == "Show the status And compare the test results" for kind, text in rig.output.log if kind == "said") == 1


async def test_queued_news_cannot_replace_eligible_pending_follow_up_after_expiry(rig_for: MakeRig) -> None:
    from tests.test_voice_mode import news_rig

    gate = asyncio.Event()
    decisions = Decisions([("floor",)], gate)
    rig, arrivals = await news_rig(rig_for, [Text("The build is green.")], [Text("The tests pass.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(5.9)
    rig.audio.say("And the tests?")
    await until(lambda: len(decisions.candidates) == 1)
    arrivals[0].set()
    await rig.advance(.2)
    assert rig.state == "idle"
    assert "The typo fix is up." not in rig.player.texts
    gate.set()
    await until(lambda: len(rig.user_messages()) == 3)
    assert "And the tests?" in rig.user_messages()[-1]


async def test_late_playback_speech_cannot_enter_the_raw_follow_up_path(rig_for: MakeRig) -> None:
    from tests.test_voice_mode import STORY, speaking

    decisions = Decisions([("ignore",)])
    rig = await rig_for(STORY, [Text("Must not run.")], recognition=decisions)
    await speaking(rig)
    onset, candidate = rig.clock.now(), rig.audio.candidate + 1
    await rig.advance(.1)
    rig.player.finish()
    await rig.advance(.1)
    await until(lambda: rig.state == "follow_up")
    rig.audio.emit(SpeechStarted(rig.clock.now(), rig.audio.owner, candidate, onset))
    rig.audio.emit(Heard("Lena, change that.", None, rig.clock.now(), rig.audio.owner, candidate, onset))
    await rig.settle()
    assert len(rig.user_messages()) == 1 and len(decisions.candidates) == 1
    assert decisions.candidates[0].phase in ("speaking", "announce")


async def test_late_announcement_speech_cannot_enter_the_raw_follow_up_path(rig_for: MakeRig) -> None:
    from tests.test_voice_mode import NEWS, news_rig

    decisions = Decisions([("ignore",)])
    rig, [arrival] = await news_rig(rig_for, [Text(NEWS)], [Text("Must not run.")], recognition=decisions)
    rig.player.hold = True
    arrival.set()
    await until(lambda: rig.state == "announce" and NEWS in rig.player.texts)
    await rig.advance(.1)
    onset, candidate = rig.clock.now(), rig.audio.candidate + 1
    await rig.advance(.1)
    rig.player.finish()
    await rig.advance(.1)
    await until(lambda: rig.state == "follow_up")
    rig.audio.emit(SpeechStarted(rig.clock.now(), rig.audio.owner, candidate, onset))
    rig.audio.emit(Heard("Lena, change that.", None, rig.clock.now(), rig.audio.owner, candidate, onset))
    await rig.settle()
    assert len(rig.user_messages()) == 1 and len(decisions.candidates) == 1
    assert decisions.candidates[0].phase in ("speaking", "announce")


async def test_long_thinking_onset_survives_natural_expiry_and_excludes_future_onsets(rig_for: MakeRig, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.fakes import Gate, ResolveScope

    answer = asyncio.Event()
    decisions = Decisions([("floor",)])
    rig = await rig_for([Gate(answer), Text("The build is green.")],
                        [ResolveScope("preserve_tasks"), Text("The tests pass too.")], recognition=decisions)
    capture = rig.audio.capture
    settings: list[tuple[bool, bool]] = []

    def recording_capture(on: bool, *, owner: int | None = None, idle: bool = False, wake_candidate: int | None = None) -> bool:
        settings.append((on, idle))
        return capture(on, owner=owner, idle=idle, wake_candidate=wake_candidate)

    monkeypatch.setattr(rig.audio, "capture", recording_capture)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "thinking")
    await rig.advance(.1)
    onset, owner = rig.clock.now(), rig.audio.owner
    rig.audio.speech()
    candidate = rig.audio.candidate
    await rig.advance(.1)
    answer.set()
    await until(lambda: rig.state == "follow_up")
    await rig.advance(6.1)
    await until(lambda: rig.state == "idle")
    assert settings[-1] == (True, False)
    rig.audio.say("Lena, dinner is ready.")
    rig.audio.emit(Heard("And compare all test results", None, rig.clock.now(), owner, candidate, onset))
    await until(lambda: ("said", "Show the status And compare all test results") in rig.output.log)
    assert decisions.candidates[0].observed_segments == ("And compare all test results",)
    assert sum(text == "Show the status And compare all test results" for kind, text in rig.output.log if kind == "said") == 1


@pytest.mark.parametrize("terminal", ["floor", "ignore", "uncertain", "abort", "limited", "retention"])
async def test_long_thinking_source_defers_news_until_terminal_cleanup(rig_for: MakeRig, terminal: str) -> None:
    from jarvis_hub.voice.state import CaptureAborted
    from tests.fakes import Gate, ResolveScope, Step
    from tests.test_voice_mode import news_rig

    answer = asyncio.Event()
    action: SegmentAction = "floor" if terminal == "floor" else "ignore" if terminal == "ignore" else "uncertain"
    decisions = Decisions([(action,)])
    responses: list[list[Step]] = [[ResolveScope("preserve_tasks"), Text("The tests pass too.")], [Text("Queued news.")]] if terminal == "floor" else [[Text("Queued news.")]]
    rig, [arrival] = await news_rig(rig_for, [Gate(answer), Text("The build is green.")], *responses, recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "thinking")
    await rig.advance(.1)
    onset, owner = rig.clock.now(), rig.audio.owner
    rig.audio.speech()
    candidate = rig.audio.candidate
    await rig.advance(.1)
    answer.set()
    await until(lambda: rig.state == "follow_up")
    arrival.set()
    await rig.advance(6.1)
    assert "Queued news." not in rig.player.texts
    if terminal == "abort":
        rig.audio.emit(CaptureAborted(rig.clock.now(), "overflow", owner, candidate))
    else:
        words = "x" * 6001 if terminal == "retention" else "And compare all test results"
        rig.audio.emit(Heard(words, None, rig.clock.now(), owner, candidate, onset, limited=terminal == "limited"))
    if terminal == "floor":
        await until(lambda: ("said", "Show the status And compare all test results") in rig.output.log)
        await rig.advance(6.1)
    await until(lambda: "Queued news." in rig.player.texts)
    if terminal != "floor":
        assert not any("And compare" in text for kind, text in rig.output.log if kind == "said")
    assert len(decisions.candidates) == (0 if terminal in ("abort", "limited", "retention") else 1)


async def test_typed_replacement_retires_long_thinking_source_after_expiry(rig_for: MakeRig) -> None:
    from tests.fakes import Gate

    answer = asyncio.Event()
    decisions = Decisions([("floor",)])
    rig = await rig_for([Gate(answer), Text("The build is green.")], [Text("Typed answer.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "thinking")
    await rig.advance(.1)
    onset, owner = rig.clock.now(), rig.audio.owner
    rig.audio.speech()
    candidate = rig.audio.candidate
    await rig.advance(.1)
    answer.set()
    await until(lambda: rig.state == "follow_up")
    await rig.advance(6.1)
    rig.typist.type("Tell me the time")
    await until(lambda: len(rig.user_messages()) == 2)
    rig.audio.emit(Heard("And compare all test results", None, rig.clock.now(), owner, candidate, onset))
    await rig.settle()
    assert not decisions.candidates and len(rig.user_messages()) == 2


@pytest.mark.parametrize("action", ["ignore", "uncertain", "floor"])
async def test_empty_fragment_releases_quiet_after_superseded_terminal_assessment(rig_for: MakeRig, action: SegmentAction) -> None:
    from tests.test_voice_mode import news_rig

    gate = asyncio.Event()
    decisions = Decisions([(action,)], gate)
    rig, [arrival] = await news_rig(rig_for, [Text("The build is green.")], [Text("Queued news.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(5)
    rig.audio.say("Maybe dinner later.")
    await until(lambda: len(decisions.candidates) == 1)
    await rig.advance(.5)
    rig.audio.say("")
    arrival.set()
    await rig.advance(.6)
    gate.set()
    await until(lambda: "Queued news." in rig.player.texts)
    assert len(rig.user_messages()) == 2


@pytest.mark.parametrize("rejected", [1, 65, None])
async def test_rejected_future_onset_cannot_reenter_after_earlier_thinking_admission(rig_for: MakeRig, rejected: int | None) -> None:
    from tests.fakes import Gate, ResolveScope, Step
    from jarvis_hub.voice.state import CaptureAborted

    answer, second = asyncio.Event(), asyncio.Event()
    decisions = Decisions([("floor",), ("floor",)])
    responses: list[list[Step]] = [[ResolveScope("preserve_tasks"), Text("Newer answer.")], [Text("Fresh answer.")]] if rejected == 1 else [[Text("Fresh answer.")]]
    rig = await rig_for([Gate(answer), Text("The build is green.")],
                        [ResolveScope("preserve_tasks"), Gate(second), Text("The tests pass too.")], *responses, recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "thinking")
    await rig.advance(.1)
    onset, owner = rig.clock.now(), rig.audio.owner
    rig.audio.speech()
    candidate = rig.audio.candidate
    await rig.advance(.1)
    answer.set()
    await until(lambda: rig.state == "follow_up")
    await rig.advance(6.1)
    if rejected is None:
        rig.audio.emit(SpeechStarted(rig.clock.now(), owner, None, rig.clock.now()))
        future = None
    else:
        for _ in range(rejected):
            rig.audio.speech()
        future = rig.audio.candidate
    future_onset = rig.clock.now()
    await rig.settle()
    rig.audio.emit(Heard("And compare all test results", None, rig.clock.now(), owner, candidate, onset))
    await until(lambda: ("said", "Show the status And compare all test results") in rig.output.log)
    rig.audio.emit(Heard("Lena, dinner is ready.", None, rig.clock.now(), owner, future, future_onset))
    await rig.settle()
    assert len(decisions.candidates) == 1
    if rejected == 1:
        newer = asyncio.Event()
        decisions.gate = newer
        rig.audio.say("And the SDK too")
        await until(lambda: len(decisions.candidates) == 2)
        rig.audio.emit(CaptureAborted(rig.clock.now(), "overflow", owner, future))
        await rig.settle()
        newer.set()
        second.set()
        await until(lambda: ("said", "Show the status And compare all test results And the SDK too") in rig.output.log)
    else:
        rig.audio.say("Jarvis, this unknown source must wait.")
        await rig.settle()
        assert len(decisions.candidates) == 1
        assert sum(kind == "notice" and "fresh wake" in text for kind, text in rig.output.log) == 1
    second.set()
    await until(lambda: rig.state == "follow_up")
    await rig.advance(6.1)
    rig.audio.wake()
    rig.audio.say("Fresh request")
    await until(lambda: ("said", "Fresh request") in rig.output.log)
    assert len(decisions.candidates) == (2 if rejected == 1 else 1)


@pytest.mark.parametrize("ending", ["That's all.", "Basta così."])
async def test_contextually_admitted_whole_end_control_closes_silently_without_changing_work(
    rig_for: MakeRig, tmp_path: Path, ending: str,
) -> None:
    from tests.test_voice_mode import news_rig, talker_received

    gate = asyncio.Event()
    decisions = Decisions([("floor",)], gate)
    state = tmp_path / "state.json"
    rig, _ = await news_rig(rig_for, [Text("The build is green.")], [Text("Must not run.")],
                            state_path=state, recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "follow_up")
    await rig.settle()
    before = json.loads(state.read_text())
    assert before["tasks"]
    turns, messages, playback = len(talker_received(rig)), rig.user_messages(), rig.player.events.copy()
    rig.audio.say(ending)
    await until(lambda: len(decisions.candidates) == 1)
    await rig.settle()
    assert rig.state == "follow_up"
    gate.set()
    await rig.settle()

    assert rig.state == "idle"
    assert rig.user_messages() == messages
    assert len(talker_received(rig)) == turns
    assert rig.player.events == playback
    after = json.loads(state.read_text())
    assert after["tasks"] == before["tasks"]
    assert after["spoken_scopes"] == before["spoken_scopes"]
    assert after["approvals"] == before["approvals"]


@pytest.mark.parametrize("action", ["ignore", "uncertain"])
@pytest.mark.parametrize("ending", ["That's all.", "Basta così.", "Lena, that's all."])
async def test_unadmitted_end_words_cannot_close_a_follow_up(
    rig_for: MakeRig, action: SegmentAction, ending: str,
) -> None:
    decisions = Decisions([(action,)])
    rig = await rig_for([Text("The build is green.")], [Text("Must not run.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "follow_up")
    rig.audio.say(ending)
    await until(lambda: len(decisions.candidates) == 1)
    await rig.settle()
    assert rig.state == "follow_up" and len(rig.user_messages()) == 1
    await rig.advance(6)
    assert rig.state == "idle"


@pytest.mark.parametrize("utterance", ["Thanks Jarvis, check the CI", "Grazie Jarvis, controlla le PR"])
async def test_admitted_longer_follow_up_keeps_end_words_as_raw_request(rig_for: MakeRig, utterance: str) -> None:
    decisions = Decisions([("floor",)])
    rig = await rig_for([Text("The build is green.")], [Text("The tests pass.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "follow_up")
    rig.audio.say(utterance)
    await until(lambda: len(rig.user_messages()) == 2)
    assert f"Show the status {utterance}</user>" in rig.user_messages()[-1]


async def test_only_selected_whole_follow_up_text_controls_silent_end(rig_for: MakeRig) -> None:
    decisions = Decisions([("uncertain",), ("ignore", "floor")])
    rig = await rig_for([Text("The build is green.")], [Text("Must not run.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "follow_up")
    rig.audio.say("Lena, dinner is ready.")
    await until(lambda: len(decisions.candidates) == 1)
    await rig.settle()
    rig.audio.say("That's all.")
    await until(lambda: len(decisions.candidates) == 2)
    await rig.settle()
    assert decisions.candidates[-1].observed_segments == ("Lena, dinner is ready.", "That's all.")
    assert rig.state == "idle" and len(rig.user_messages()) == 1


@pytest.mark.parametrize("ending", ["That's all.", "Basta così."])
async def test_eligible_postreply_end_control_delivered_after_expiry_still_ends_silently(
    rig_for: MakeRig, ending: str,
) -> None:
    from tests.test_voice_mode import talker_received

    decisions = Decisions([("floor",)])
    rig = await rig_for([Text("The build is green.")], [Text("Must not run.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Show the status")
    await until(lambda: rig.state == "follow_up")
    await rig.advance(5.9)
    onset, owner, candidate = rig.clock.now(), rig.audio.owner, rig.audio.candidate + 1
    await rig.advance(.2)
    assert rig.state == "idle"
    turns, playback = len(talker_received(rig)), rig.player.events.copy()
    rig.audio.emit(SpeechStarted(rig.clock.now(), owner, candidate, onset))
    rig.audio.emit(Heard(ending, None, rig.clock.now(), owner, candidate, onset))
    await until(lambda: len(decisions.candidates) == 1)
    await rig.settle()
    assert decisions.candidates[0].phase == "follow_up"
    assert rig.state == "idle" and len(rig.user_messages()) == 1
    assert len(talker_received(rig)) == turns and rig.player.events == playback


@pytest.mark.parametrize("origin", ["speaking", "announce"])
@pytest.mark.parametrize("action", ["floor", "stop"])
async def test_playback_origin_end_words_delivered_in_follow_up_keep_contextual_semantics(
    rig_for: MakeRig, origin: str, action: SegmentAction,
) -> None:
    from tests.test_voice_mode import NEWS, STORY, news_rig, speaking

    decisions = Decisions([(action,)])
    if origin == "announce":
        rig, [arrival] = await news_rig(rig_for, [Text(NEWS)], [Text("A new answer.")], recognition=decisions)
        rig.player.hold = True
        arrival.set()
        await until(lambda: rig.state == "announce" and NEWS in rig.player.texts)
    else:
        rig = await rig_for(STORY, [Text("A new answer.")], recognition=decisions)
        await speaking(rig)
    await rig.advance(.1)
    onset, owner, candidate = rig.clock.now(), rig.audio.owner, rig.audio.candidate + 1
    await rig.advance(.1)
    rig.player.finish()
    await rig.advance(.1)
    await until(lambda: rig.state == "follow_up")
    rig.audio.emit(SpeechStarted(rig.clock.now(), owner, candidate, onset))
    rig.audio.emit(Heard("That's all.", None, rig.clock.now(), owner, candidate, onset))
    await until(lambda: len(decisions.candidates) == 1)
    await rig.settle()
    assert decisions.candidates[0].phase == origin
    if action == "floor":
        expected = "Tell me a story That's all." if origin == "speaking" else "That's all."
        assert ("said", expected) in rig.output.log
        assert any(f"{expected}</user>" in message for message in rig.backend.session("talker").received)
        assert "A new answer." in rig.player.texts
    else:
        assert rig.state == ("follow_up" if origin == "speaking" else "idle")
        assert len(rig.user_messages()) == 1
        assert "A new answer." not in rig.player.texts
