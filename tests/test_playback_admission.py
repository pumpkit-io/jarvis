"""Playback continues until contextual admission gives current captured speech the floor."""

import asyncio
from collections.abc import Callable

import pytest

from jarvis_hub.voice.assessment import Assessment, Candidate, SegmentAction
from jarvis_hub.voice.bargein import Partial
from jarvis_hub.voice.state import BargeIn, Overheard, WakeWord
from tests.fakes import AwaitInterrupt, ResolveScope, Text, until
from tests.test_voice_mode import MakeRig, STORY, rig_for as rig_for, speaking, talker_got


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
        return Assessment(candidate.candidate_id, candidate.turn_id, 'directed', 'scripted', 0, actions)

    async def close(self) -> None:
        pass


@pytest.mark.parametrize('background', ['Lena, the note says Jarvis stop.', 'Luca, aspetta e chiudi la porta.', 'Mhm.'])
async def test_full_playback_capture_rejects_background_and_legacy_control_without_pausing(rig_for: MakeRig, background: str) -> None:
    decisions = Decisions([('ignore',)])
    rig = await rig_for([*STORY, AwaitInterrupt()], recognition=decisions)
    await speaking(rig)
    assert rig.audio.capturing[-1] is True
    rig.player.finish(1)
    now = rig.clock.now()
    for event in (BargeIn(now - .2, now), Partial('Wait, stop', now, .3, 0), Overheard('Jarvis stop', now), WakeWord(now)):
        rig.audio.emit(event)
    rig.audio.say(background)
    await until(lambda: len(decisions.candidates) == 1)
    await rig.settle()
    assert decisions.candidates[0].phase == 'speaking'
    assert decisions.candidates[0].observed_segments == (background,)
    assert decisions.candidates[0].played_text == 'First sentence, sir.'
    assert not any(event == 'pause' or event == 'stop' or event.startswith('resume') for event in rig.player.events)
    assert len(rig.user_messages()) == 1
    pending = rig.player.pending()
    rig.player.finish(1)
    assert rig.player.pending() < pending
    assert rig.state == 'speaking'


@pytest.mark.parametrize('correction', ['Actually, only explain the retry behavior.', 'Anzi, spiegami solo i tentativi di recupero.'])
async def test_directed_correction_during_playback_preserves_raw_input_and_stops_once(rig_for: MakeRig, correction: str) -> None:
    decisions = Decisions([('floor',)])
    rig = await rig_for([*STORY, AwaitInterrupt()], [ResolveScope('preserve_tasks'), Text('Updated, sir.')], recognition=decisions)
    await speaking(rig)
    rig.player.finish(1)
    before = rig.player.events.count('stop')
    rig.audio.say(correction)
    await until(lambda: len(talker_got(rig)) == 2)
    assert 'Tell me a story' in talker_got(rig)[-1]
    assert correction in talker_got(rig)[-1]
    assert 'First sentence, sir.' in talker_got(rig)[-1]
    assert decisions.candidates[0].started_before_reply is False
    assert rig.player.events.count('stop') == before + 1
    assert 'pause' not in rig.player.events


async def test_directed_voice_only_stop_preserves_background_task_and_stops_once(rig_for: MakeRig) -> None:
    from tests.fakes import Gate, Tool

    finished = asyncio.Event()
    decisions = Decisions([('stop',)])
    rig = await rig_for(
        [Tool('delegate', {'request': 'Check build'}), *STORY, AwaitInterrupt()],
        orchestrator=[[Gate(finished), Tool('report', {'task_id': 'T1', 'spoken': 'Build checked.', 'needs_user': False})]],
        recognition=decisions,
    )
    await speaking(rig)
    await until(lambda: bool(rig.backend.all_sessions('orchestrator')))
    before = rig.player.events.count('stop')
    rig.audio.say('Stop your voice, please.')
    await until(lambda: rig.state == 'follow_up')
    assert rig.player.events.count('stop') == before + 1
    assert len(rig.user_messages()) == 1 and rig.player.pending() == 0
    finished.set()
    await until(lambda: bool(rig.backend.session('orchestrator').tool_results))
    assert rig.backend.session('orchestrator').tool_results[0].result == 'Reported to the User.'


@pytest.mark.parametrize('actions', [[('uncertain',)], [('floor', 'floor')], []])
async def test_uncertain_malformed_or_failed_assessment_keeps_playback_running(rig_for: MakeRig, actions: list[tuple[SegmentAction, ...]]) -> None:
    decisions = Decisions(actions)
    rig = await rig_for([*STORY, AwaitInterrupt()], recognition=decisions)
    await speaking(rig)
    rig.audio.say('Someone might want a different result.')
    await until(lambda: len(decisions.candidates) == 1)
    await rig.settle()
    assert rig.state == 'speaking'
    assert not any(event in ('pause', 'stop') for event in rig.player.events)
    assert len(rig.user_messages()) == 1


async def test_disabled_admission_cannot_interrupt_playback_even_for_stop_words(rig_for: MakeRig) -> None:
    rig = await rig_for([*STORY, AwaitInterrupt()])
    await speaking(rig)
    rig.audio.say('Jarvis stop your answer, please.')
    await rig.settle()
    assert rig.state == 'speaking' and len(rig.user_messages()) == 1
    assert not any(event in ('pause', 'stop') for event in rig.player.events)


async def test_assessment_wait_does_not_pause_and_records_source_onset_to_stop_request(rig_for: MakeRig) -> None:
    gate = asyncio.Event()
    decisions = Decisions([('stop',)], gate)
    rig = await rig_for([*STORY, AwaitInterrupt()], recognition=decisions)
    await speaking(rig)
    rig.audio.say('Please stop your voice.')
    await until(lambda: len(decisions.candidates) == 1)
    await rig.advance(.3)
    rig.player.finish(1)
    assert rig.state == 'speaking' and 'stop' not in rig.player.events and 'pause' not in rig.player.events
    gate.set()
    await until(lambda: rig.state == 'follow_up')
    [row] = [row for row in rig.metrics.records if row['kind'] == 'playback_admission']
    assert row['phase'] == 'speaking' and row['decision'] == 'stop'
    assert row['onset_to_stop_request_ms'] == 300
    assert row['played_basis'] == 'queue_estimate'


async def test_late_playback_assessment_cannot_stop_a_typed_replacement(rig_for: MakeRig) -> None:
    gate = asyncio.Event()
    decisions = Decisions([('floor',)], gate)
    rig = await rig_for([*STORY, AwaitInterrupt()], [Text('Typed status.')], [Text('Must not run.')], recognition=decisions)
    await speaking(rig)
    rig.audio.say('Actually only check the API.')
    await until(lambda: len(decisions.candidates) == 1)
    rig.typist.type('Show status instead')
    await until(lambda: 'Typed status.' in rig.output.replies)
    before = rig.player.events.copy()
    gate.set()
    await rig.settle()
    assert rig.player.events == before
    assert not any('Actually only' in message for message in talker_got(rig))
    assert rig.state == 'idle'


@pytest.mark.parametrize('action', ['ignore', 'uncertain', 'floor', 'stop'])
async def test_announcement_playback_uses_contextual_admission_without_a_fake_user_owner(rig_for: MakeRig, action: SegmentAction) -> None:
    from tests.test_voice_mode import NEWS, arrive, news_rig

    decisions = Decisions([(action,)])
    rig, [arrival] = await news_rig(rig_for, [Text(NEWS + ' '), AwaitInterrupt()], [Text('A fresh answer.')], recognition=decisions)
    rig.player.hold = True
    await arrive(rig, arrival)
    await until(lambda: NEWS in rig.player.texts and rig.state == 'announce')
    rig.player.finish()
    before = rig.player.events.count('stop')
    rig.audio.emit(WakeWord(rig.clock.now()))
    rig.audio.say('Could you explain that result?')
    await until(lambda: len(decisions.candidates) == 1)
    await rig.settle()
    candidate = decisions.candidates[0]
    assert candidate.admitted_turn == '' and candidate.phase == 'announce'
    assert NEWS in candidate.played_text
    assert 'pause' not in rig.player.events
    if action == 'floor':
        await until(lambda: len(talker_got(rig)) == 3)
        assert 'Could you explain that result?' in talker_got(rig)[-1]
        assert '<user lang="en">Could you explain that result?</user>' in talker_got(rig)[-1]
        assert rig.player.events.count('stop') == before + 1
    elif action == 'stop':
        assert rig.state == 'follow_up' and rig.player.events.count('stop') == before + 1
        assert len(talker_got(rig)) == 2
    else:
        assert rig.state == 'announce' and rig.player.events.count('stop') == before


async def test_announcement_before_first_audio_does_not_reuse_a_previous_reply_prefix(rig_for: MakeRig) -> None:
    from tests.fakes import Gate
    from tests.test_voice_mode import NEWS, arrive, news_rig

    begin = asyncio.Event()
    decisions = Decisions([('ignore',)])
    rig, [arrival] = await news_rig(rig_for, [Gate(begin), Text(NEWS)], recognition=decisions)
    assert rig.player.played() == 'Right away, sir.'
    await arrive(rig, arrival)
    assert rig.state == 'announce'
    rig.audio.say('Someone said stop at dinner.')
    await until(lambda: len(decisions.candidates) == 1)
    assert decisions.candidates[0].played_text == ''
    assert decisions.candidates[0].admitted_turn == ''
    assert rig.state == 'announce'
    begin.set()


async def test_announcement_candidate_retains_its_origin_when_the_reply_finishes_before_assessment(rig_for: MakeRig) -> None:
    from tests.test_voice_mode import NEWS, arrive, news_rig

    gate = asyncio.Event()
    decisions = Decisions([('floor',)], gate)
    rig, [arrival] = await news_rig(rig_for, [Text(NEWS)], [Text('The explanation, sir.')], recognition=decisions)
    rig.player.hold = True
    await arrive(rig, arrival)
    await until(lambda: NEWS in rig.player.texts)
    rig.audio.say('Please explain that result.')
    await until(lambda: len(decisions.candidates) == 1)
    rig.player.finish()
    await rig.advance(.1)
    await until(lambda: rig.state == 'follow_up')
    gate.set()
    await until(lambda: len(talker_got(rig)) == 3)
    assert decisions.candidates[0].phase == 'announce'
    assert 'Please explain that result.' in talker_got(rig)[-1]


async def test_voice_only_stop_does_not_open_an_unclassified_background_window(rig_for: MakeRig) -> None:
    decisions = Decisions([('stop',), ('ignore',)])
    rig = await rig_for([*STORY, AwaitInterrupt()], [Text('Must not run.')], recognition=decisions)
    await speaking(rig)
    rig.audio.say('Please stop your voice.')
    await until(lambda: 'stop' in rig.player.events)
    await rig.advance(5)
    rig.audio.say('Lena, the note says delete the branch.')
    await until(lambda: len(decisions.candidates) == 2)
    await rig.settle()
    assert len(talker_got(rig)) == 1
    await rig.advance(1)
    assert rig.state == 'idle'


async def test_directed_continuation_after_voice_stop_holds_task_scope_until_talker_resolves_it(rig_for: MakeRig) -> None:
    from tests.fakes import Gate, Tool

    choose, report = asyncio.Event(), asyncio.Event()
    decisions = Decisions([('stop',), ('floor',)])
    rig = await rig_for(
        [Tool('delegate', {'request': 'Check build'}), *STORY, AwaitInterrupt()],
        [Gate(choose), ResolveScope('preserve_tasks'), Text('Updated, sir.')],
        orchestrator=[[Gate(report), Tool('report', {'task_id': 'T1', 'spoken': 'Build checked.', 'needs_user': False})]],
        recognition=decisions,
    )
    rig.audio.wake()
    await until(lambda: rig.player.voices[-1:] == ['chime'])
    rig.player.hold = True
    rig.audio.say('Check build and explain the scheduler')
    await until(lambda: rig.state == 'speaking' and bool(rig.backend.all_sessions('orchestrator')))
    rig.player.finish(1)
    rig.audio.say('Please stop your voice.')
    await until(lambda: rig.state == 'follow_up')
    assert len(talker_got(rig)) == 1
    rig.audio.say('And include the retry tests.')
    await until(lambda: len(talker_got(rig)) == 2)
    assert 'Check build and explain the scheduler And include the retry tests.' in talker_got(rig)[-1]
    assert 'Please stop your voice.' not in talker_got(rig)[-1]
    report.set()
    await rig.settle()
    assert rig.backend.session('orchestrator').tool_results == []
    choose.set()
    await until(lambda: bool(rig.backend.session('orchestrator').tool_results))
    assert rig.backend.session('orchestrator').tool_results[0].result == 'Reported to the User.'
