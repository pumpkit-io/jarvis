"""Keyboard control is local even when speech classification cannot complete."""

import asyncio
import io

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from jarvis_hub.terminal_input import TerminalInput
from jarvis_hub.voice.state import Hypothesis
from tests.fakes import AwaitInterrupt, Gate, Text, Tool, until
from tests.test_cumulative_voice import ScriptedJev
from tests.test_voice_mode import MakeRig, STORY, rig_for as rig_for, speaking, talker_got


async def test_keyboard_stop_drains_immediately_during_blocked_jev_and_preserves_tasks_and_typing(rig_for: MakeRig) -> None:
    report = asyncio.Event()
    jev = ScriptedJev()
    rig = await rig_for([Tool('delegate', {'request': 'Check build'}), *STORY, AwaitInterrupt()], [Text('Typed status.')],
                        orchestrator=[[Gate(report), Tool('report', {'task_id': 'T1', 'spoken': 'Build checked.', 'needs_user': False})]],
                        recognition=jev)
    await speaking(rig)
    await until(lambda: bool(rig.backend.all_sessions('orchestrator')))
    rig.audio.speech()
    source, owner, now = rig.audio.candidate, rig.audio.owner, rig.clock.now()
    rig.audio.emit(Hypothesis('Could you wait', now, owner, source, 1, now, now))
    await until(lambda: len(jev.snapshots) == 1)
    with create_pipe_input() as pipe:
        with TerminalInput(io.StringIO(), io.StringIO(), io.StringIO(), rig.clock, input=pipe, output=DummyOutput()) as terminal:
            lines = terminal.lines(lambda: rig.voice.local_stop(rig.hub.request_interrupt))
            reading = asyncio.ensure_future(anext(lines))
            pipe.send_text('Show status instead')
            pipe.send_bytes(b'\x07')
            await until(lambda: rig.player.pending() == 0 and 'stop' in rig.player.events)
            assert rig.clock.now() == now
            assert not reading.done() and len(talker_got(rig)) == 1
            report.set()
            await until(lambda: bool(rig.backend.session('orchestrator').tool_results))
            assert rig.backend.session('orchestrator').tool_results[0].result == 'Reported to the User.'
            pipe.send_text('\r')
            line = await asyncio.wait_for(reading, timeout=1)
            rig.typist.type(line.text)
            await until(lambda: 'Typed status.' in rig.output.replies)
            before = rig.player.events.count('stop')
            jev.answer(floor=True, ready=True)
            await rig.settle()
            assert rig.player.events.count('stop') == before and len(rig.user_messages()) == 2
            assert 'Typed status.' not in rig.player.texts
            assert not any('Could you wait' in text for text in talker_got(rig))
            await lines.aclose()
