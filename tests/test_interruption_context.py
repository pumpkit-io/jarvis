"""Interruption context retains its playback-estimate boundary at the Hub seam."""

import asyncio
import xml.etree.ElementTree as ET

from tests.fakes import AwaitInterrupt, Gate, ResolveScope, Text, until
from tests.test_voice_mode import DelayedRelation, MakeRig, rig_for as rig_for, talker_got


async def test_delayed_correction_labels_only_the_played_prefix_as_a_queue_estimate(rig_for: MakeRig) -> None:
    relation = DelayedRelation()
    begin_reply = asyncio.Event()
    rig = await rig_for(
        [Gate(begin_reply), Text("First sentence, sir. Second sentence, sir. "), AwaitInterrupt()],
        [ResolveScope("preserve_tasks"), Text("Updated, sir.")], recognition=relation,
    )
    rig.audio.wake()
    await until(lambda: rig.player.voices[-1:] == ["chime"])
    rig.player.hold = True
    rig.audio.say("Explain the scheduler")
    await until(lambda: len(talker_got(rig)) == 1)
    rig.audio.say("including retries")
    await until(lambda: len(relation.candidates) == 1)
    begin_reply.set()
    await until(lambda: len(rig.player.clips) >= 3)
    rig.player.finish(1)
    relation.release.set()
    await until(lambda: len(talker_got(rig)) == 2)

    context = ET.fromstring(talker_got(rig)[1].split("\n", 1)[0])
    assert context.attrib == {"spoken": "First sentence, sir.", "basis": "queue_estimate"}
    assert "Second sentence" not in context.attrib["spoken"]
    assert "Explain the scheduler including retries" in talker_got(rig)[1]


async def test_old_keyword_cannot_reopen_listening_after_a_newer_typed_request(rig_for: MakeRig) -> None:
    from jarvis_hub.voice.state import WakeWord

    finish = asyncio.Event()
    rig = await rig_for([Gate(finish), Text("Typed status, sir.")])
    old_onset = rig.clock.now()
    await rig.advance(1)
    rig.typist.type("Show the status")
    await until(lambda: len(talker_got(rig)) == 1)
    rig.audio.emit(WakeWord(old_onset))
    await rig.settle()
    assert rig.state == "idle"
    assert "chime" not in rig.player.voices
    finish.set()
    await until(lambda: "Typed status, sir." in rig.output.replies)
    assert "Typed status, sir." not in rig.player.texts

    await rig.advance(1)
    rig.audio.wake()
    await until(lambda: rig.state == "listening")
    assert rig.player.voices[-1] == "chime"
