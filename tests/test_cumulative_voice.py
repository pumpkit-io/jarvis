"""Cumulative decisions stop output before a correction is ready to submit."""

import asyncio

import pytest
from collections.abc import Callable

from jarvis_hub.voice.cumulative import Decision, Snapshot
from jarvis_hub.voice.state import Heard, Hypothesis
from tests.fakes import AwaitInterrupt, ResolveScope, Text, until
from tests.test_voice_mode import MakeRig, STORY, rig_for as rig_for, speaking, talker_got


class ScriptedJev:
    cumulative = True

    def __init__(self) -> None:
        self.snapshots: list[Snapshot] = []
        self.replies: asyncio.Queue[Decision] = asyncio.Queue()

    async def assess_snapshot(self, snapshot: Snapshot) -> Decision:
        self.snapshots.append(snapshot)
        return await self.replies.get()

    async def assess(self, candidate: object, *, current: Callable[[], tuple[str, str]], timeout_s: float = 2.0) -> object:
        raise AssertionError('Cumulative speech must not use endpoint admission')

    async def close(self) -> None:
        pass

    def answer(self, *, floor: bool = False, ready: bool = False, voice_only: bool = False, directed: bool = True) -> None:
        snapshot = self.snapshots[-1]
        self.replies.put_nowait(Decision(snapshot.id, tuple(source.key for source in snapshot.sources) if directed else (),
                                       floor, ready, voice_only, 'assessed'))


async def test_unfinished_directed_correction_stops_before_endpoint_without_submitting_or_losing_words(rig_for: MakeRig) -> None:
    jev = ScriptedJev()
    rig = await rig_for([*STORY, AwaitInterrupt()], [ResolveScope('preserve_tasks'), Text('Corrected, sir.')], recognition=jev)
    await speaking(rig)
    rig.audio.speech()
    now, source = rig.clock.now(), rig.audio.candidate
    rig.audio.emit(Hypothesis('No, wait, I meant', now, rig.audio.owner, source, 1, now, now))
    await until(lambda: len(jev.snapshots) == 1)
    jev.answer(floor=True)
    await until(lambda: 'stop' in rig.player.events)
    assert rig.player.pending() == 0 and len(talker_got(rig)) == 1
    assert 'pause' not in rig.player.events
    assert rig.audio.capturing[-1] is True
    rig.audio.emit(Heard('No, wait, I meant the other repository.', 'en', now, rig.audio.owner, source, now))
    await until(lambda: len(jev.snapshots) == 2)
    assert jev.snapshots[-1].sources[0].text == 'No, wait, I meant the other repository.'
    jev.answer(floor=True, ready=True)
    await until(lambda: len(talker_got(rig)) == 2)
    assert rig.user_messages()[-1].count('No, wait, I meant the other repository.') == 1
    assert rig.player.events.count('stop') == 1


async def test_voice_only_stop_drains_output_without_canceling_a_background_task(rig_for: MakeRig) -> None:
    from tests.fakes import Gate, Tool

    finished = asyncio.Event()
    jev = ScriptedJev()
    rig = await rig_for(
        [Tool('delegate', {'request': 'Check build'}), *STORY, AwaitInterrupt()],
        orchestrator=[[Gate(finished), Tool('report', {'task_id': 'T1', 'spoken': 'Build checked.', 'needs_user': False})]],
        recognition=jev,
    )
    await speaking(rig)
    await until(lambda: bool(rig.backend.all_sessions('orchestrator')))
    rig.audio.say('Stop your voice, please.')
    await until(lambda: len(jev.snapshots) == 1)
    jev.answer(floor=True, voice_only=True)
    await until(lambda: rig.player.pending() == 0)
    assert len(rig.user_messages()) == 1 and rig.state == 'follow_up'
    finished.set()
    await until(lambda: bool(rig.backend.session('orchestrator').tool_results))
    assert rig.backend.session('orchestrator').tool_results[0].result == 'Reported to the User.'


async def test_changed_hypotheses_coalesce_while_busy_and_obsolete_floor_decision_cannot_stop(rig_for: MakeRig) -> None:
    jev = ScriptedJev()
    rig = await rig_for([*STORY, AwaitInterrupt()], recognition=jev)
    await speaking(rig)
    rig.audio.speech()
    now, source = rig.clock.now(), rig.audio.candidate
    rig.audio.emit(Hypothesis('Wait', now, rig.audio.owner, source, 1, now, now))
    await until(lambda: len(jev.snapshots) == 1)
    for revision, text in enumerate(('Waiter', 'Waiter, the bill', 'Waiter, the bill please.'), 2):
        rig.audio.emit(Hypothesis(text, now, rig.audio.owner, source, revision, now, now))
    await rig.settle()
    assert len(jev.snapshots) == 1
    jev.answer(floor=True)
    await until(lambda: len(jev.snapshots) == 2)
    assert jev.snapshots[-1].sources[0].text == 'Waiter, the bill please.'
    assert not any(event in ('stop', 'pause') for event in rig.player.events)
    jev.answer(directed=False)
    await rig.settle()
    assert len(rig.user_messages()) == 1


async def test_silence_reassessment_uses_same_words_with_later_timing_without_idle_polling(rig_for: MakeRig) -> None:
    jev = ScriptedJev()
    rig = await rig_for([*STORY, AwaitInterrupt()], recognition=jev)
    await rig.advance(2)
    assert not jev.snapshots
    await speaking(rig)
    rig.audio.say('Perhaps the other repository')
    await until(lambda: len(jev.snapshots) == 1)
    jev.answer()
    await rig.settle()
    await rig.advance(.41)
    await until(lambda: len(jev.snapshots) == 2)
    first, second = jev.snapshots
    assert first.sources == second.sources and second.at > first.at
    assert not any(event in ('stop', 'pause') for event in rig.player.events)


async def test_late_result_after_typed_replacement_has_no_output_authority(rig_for: MakeRig) -> None:
    jev = ScriptedJev()
    rig = await rig_for([*STORY, AwaitInterrupt()], [Text('Typed status.')], recognition=jev)
    await speaking(rig)
    rig.audio.say('Actually, use the other repository.')
    await until(lambda: len(jev.snapshots) == 1)
    rig.typist.type('Show status instead')
    await until(lambda: 'Typed status.' in rig.output.replies)
    before = rig.player.events.copy()
    jev.answer(floor=True, ready=True)
    await rig.settle()
    assert rig.player.events == before and rig.state == 'idle'
    assert len(rig.user_messages()) == 2


async def test_duplicate_endpoint_cannot_submit_a_second_revision(rig_for: MakeRig) -> None:
    jev = ScriptedJev()
    rig = await rig_for([*STORY, AwaitInterrupt()], [ResolveScope('preserve_tasks'), Text('Corrected.')], recognition=jev)
    await speaking(rig)
    rig.audio.speech()
    now, source, owner = rig.clock.now(), rig.audio.candidate, rig.audio.owner
    final = Heard('Only explain the other repository.', 'en', now, owner, source, now)
    rig.audio.emit(final)
    await until(lambda: len(jev.snapshots) == 1)
    jev.answer(floor=True, ready=True)
    await until(lambda: len(talker_got(rig)) == 2)
    rig.audio.emit(final)
    await rig.settle()
    assert len(jev.snapshots) == 1 and len(talker_got(rig)) == 2


@pytest.mark.parametrize('text', ['Mhm.', 'Lena, the note says Jarvis stop.', 'Luca, aspetta e chiudi la porta.', 'Perhaps someone else meant that.'])
async def test_negative_and_uncertain_cumulative_speech_preserves_playback(rig_for: MakeRig, text: str) -> None:
    jev = ScriptedJev()
    rig = await rig_for([*STORY, AwaitInterrupt()], recognition=jev)
    await speaking(rig)
    rig.audio.say(text)
    await until(lambda: len(jev.snapshots) == 1)
    jev.answer(directed=False)
    await rig.settle()
    assert rig.state == 'speaking' and len(rig.user_messages()) == 1
    assert not any(event in ('pause', 'stop') for event in rig.player.events)


async def test_malformed_source_binding_cannot_stop_playback(rig_for: MakeRig) -> None:
    jev = ScriptedJev()
    rig = await rig_for([*STORY, AwaitInterrupt()], recognition=jev)
    await speaking(rig)
    rig.audio.say('Wait, use the other repository.')
    await until(lambda: len(jev.snapshots) == 1)
    jev.replies.put_nowait(Decision(jev.snapshots[-1].id, ((999, 999),), True, True, False, 'assessed'))
    await rig.settle()
    assert rig.state == 'speaking' and len(rig.user_messages()) == 1
    assert not any(event in ('pause', 'stop') for event in rig.player.events)


async def test_failed_observation_preserves_output_without_silence_retry_storm(rig_for: MakeRig) -> None:
    jev = ScriptedJev()
    rig = await rig_for([*STORY, AwaitInterrupt()], recognition=jev)
    await speaking(rig)
    rig.audio.say('Could you wait?')
    await until(lambda: len(jev.snapshots) == 1)
    jev.replies.put_nowait(Decision(jev.snapshots[-1].id, (), False, False, False, 'unavailable'))
    await rig.settle()
    await rig.advance(2)
    assert len(jev.snapshots) == 1 and rig.state == 'speaking'
    assert not any(event in ('pause', 'stop') for event in rig.player.events)


async def test_only_observed_directed_sources_enter_a_revision(rig_for: MakeRig) -> None:
    jev = ScriptedJev()
    rig = await rig_for([*STORY, AwaitInterrupt()], [ResolveScope('preserve_tasks'), Text('Corrected.')], recognition=jev)
    await speaking(rig)
    rig.audio.say('Lena, which repository was that?')
    await until(lambda: len(jev.snapshots) == 1)
    jev.answer(directed=False)
    await rig.settle()
    rig.audio.say('Actually, use the other repository.')
    await until(lambda: len(jev.snapshots) == 2)
    snapshot = jev.snapshots[-1]
    jev.replies.put_nowait(Decision(snapshot.id, (snapshot.sources[-1].key,), True, True, False, 'assessed',
                                   (snapshot.sources[0].key,)))
    await until(lambda: len(talker_got(rig)) == 2)
    assert 'Actually, use the other repository.' in rig.user_messages()[-1]
    assert 'Lena' not in rig.user_messages()[-1]
