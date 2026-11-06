"""Only selected original fragments enter an admitted spoken revision."""

from collections.abc import Callable
import asyncio

import pytest

from jarvis_hub.voice.assessment import Assessment, Candidate, SegmentAction
from tests.fakes import AwaitInterrupt, Text, until
from tests.test_voice_mode import MakeRig, rig_for as rig_for


class SegmentDecisions:
    def __init__(self, decisions: list[tuple[SegmentAction, ...]]) -> None:
        self.decisions = iter(decisions)
        self.candidates: list[Candidate] = []

    async def assess(self, candidate: Candidate, *, current: Callable[[], tuple[str, str]], timeout_s: float = 2.0) -> Assessment:
        self.candidates.append(candidate)
        actions = next(self.decisions)
        return Assessment(candidate.candidate_id, candidate.turn_id, "directed", "scripted", 0, segment_actions=actions)

    async def close(self) -> None:
        pass


@pytest.mark.parametrize(("opening", "background", "directed"), [
    ("Review the API", "Lena, put the keys upstairs.", "Jarvis, only review the endpoint changes."),
    ("Controlla le API", "Luca, lascia le chiavi sul tavolo.", "Jarvis, controlla solo le modifiche agli endpoint."),
])
async def test_retained_background_cannot_ride_with_a_later_directed_fragment(
    rig_for: MakeRig, opening: str, background: str, directed: str,
) -> None:
    decisions = SegmentDecisions([("uncertain",), ("ignore", "floor")])
    rig = await rig_for([AwaitInterrupt()], [Text("Updated, sir.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say(opening)
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say(background)
    await until(lambda: len(decisions.candidates) == 1)
    await rig.settle()
    rig.audio.say(directed)
    await until(lambda: len(rig.user_messages()) == 2)
    assert decisions.candidates[-1].observed_segments == (background, directed)
    assert opening in rig.user_messages()[-1] and directed in rig.user_messages()[-1]
    assert background not in rig.user_messages()[-1]


async def test_selected_joint_fragments_keep_the_opening_and_all_four_original_parts(rig_for: MakeRig) -> None:
    decisions = SegmentDecisions([("uncertain",), ("uncertain", "uncertain"),
                                  ("uncertain", "uncertain", "uncertain"), ("floor", "floor", "floor", "floor")])
    rig = await rig_for([AwaitInterrupt()], [Text("Updated, sir.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Compare")
    await until(lambda: len(rig.user_messages()) == 1)
    for index, fragment in enumerate(("the API requests", "with the command line requests", "by their latency", "for the last release"), 1):
        rig.audio.say(fragment)
        await until(lambda: len(decisions.candidates) == index)
        await rig.settle()
    await until(lambda: len(rig.user_messages()) == 2)
    assert "Compare the API requests with the command line requests by their latency for the last release" in rig.user_messages()[-1]


async def test_a_malformed_vector_does_not_interrupt_or_admit_a_tail(rig_for: MakeRig) -> None:
    decisions = SegmentDecisions([("floor", "floor")])
    rig = await rig_for([AwaitInterrupt()], [Text("Must not run.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Review the API")
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say("Jarvis, only the endpoint changes.")
    await until(lambda: len(decisions.candidates) == 1)
    await rig.settle()
    assert len(rig.user_messages()) == 1
    assert rig.state == "thinking"


async def test_stop_vector_silences_without_admitting_other_observed_speech(rig_for: MakeRig) -> None:
    decisions = SegmentDecisions([("uncertain",), ("ignore", "stop")])
    rig = await rig_for([AwaitInterrupt()], [Text("Must not run.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Review the API")
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say("Lena, carry on with dinner.")
    await until(lambda: len(decisions.candidates) == 1)
    await rig.settle()
    rig.audio.say("Jarvis, stop your answer.")
    await until(lambda: rig.state == "follow_up")
    assert len(rig.user_messages()) == 1


async def test_typed_replacement_retires_an_already_owned_request_and_late_assessment(rig_for: MakeRig) -> None:
    release = asyncio.Event()
    class Held(SegmentDecisions):
        async def assess(self, candidate: Candidate, *, current: Callable[[], tuple[str, str]], timeout_s: float = 2.0) -> Assessment:
            result = await super().assess(candidate, current=current, timeout_s=timeout_s)
            await release.wait()
            return result
    decisions = Held([("floor",)])
    rig = await rig_for([AwaitInterrupt()], [Text("Typed replacement received.")],
                        [Text("Obsolete speech must not run.")], recognition=decisions)
    rig.audio.wake()
    rig.audio.say("Review the API")
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say("Jarvis, only the endpoint changes.")
    await until(lambda: len(decisions.candidates) == 1)
    rig.clock.advance(0.1)
    rig.typist.type("Show status instead")
    await until(lambda: "Typed replacement received." in rig.output.replies)
    release.set()
    await rig.settle()
    assert rig.user_messages() == ['<user lang="en">Review the API</user>',
                                   '<user lang="en" typed="true">Show status instead</user>']
    assert rig.state == "idle" and "Typed replacement received." not in rig.player.texts
