"""Complete-thought admission through the Hub with scripted semantic decisions."""

import json
from pathlib import Path
from typing import Any, cast

import pytest

from jarvis_hub.voice.language import Lang

from jarvis_hub.voice.state import Heard, Hypothesis
from tests.fakes import Text, until
from tests.test_cumulative_voice import ScriptedJev
from tests.test_voice_mode import MakeRig, rig_for as rig_for, talker_got


CASES: dict[str, Any] = json.loads((Path(__file__).parent / 'fixtures' / 'cumulative-readiness.json').read_text())


async def test_initial_listening_assesses_before_endpoint_and_waits_for_complete_accumulated_thought(rig_for: MakeRig) -> None:
    jev = ScriptedJev(auto_initial=False)
    rig = await rig_for([Text('I will explain that, sir.')], recognition=jev)
    rig.audio.wake()
    await rig.settle()
    rig.audio.speech()
    now, source, owner = rig.clock.now(), rig.audio.candidate, rig.audio.owner
    rig.audio.emit(Hypothesis('The build passes.', now, owner, source, 1, now, now))
    await until(lambda: len(jev.snapshots) == 1)
    assert jev.snapshots[0].phase == 'listening'
    assert not talker_got(rig)
    jev.answer(ready=False)
    rig.audio.emit(Heard('The build passes.', 'en', now, owner, source, now))
    await until(lambda: len(jev.snapshots) == 2)
    jev.answer(ready=False)
    await rig.advance(3)
    await until(lambda: len(jev.snapshots) == 3)
    assert not talker_got(rig)
    jev.answer(ready=False)
    await rig.settle()
    rig.audio.say('But explain why the retry test is skipped.')
    await until(lambda: len(jev.snapshots) == 4)
    assert tuple(source.text for source in jev.snapshots[-1].sources) == ('The build passes.', 'But explain why the retry test is skipped.')
    jev.answer(ready=True)
    await until(lambda: len(talker_got(rig)) == 1)
    assert rig.user_messages() == ['<user lang="en">The build passes. But explain why the retry test is skipped.</user>']


@pytest.mark.parametrize('case', CASES['unfinished'], ids=lambda case: case['id'])
async def test_frozen_thinking_pause_intents_preserve_exact_words_without_premature_submission(
    rig_for: MakeRig, case: dict[str, Any],
) -> None:
    jev = ScriptedJev(auto_initial=False)
    rig = await rig_for([Text('Understood.')], recognition=jev)
    rig.audio.wake()
    await rig.settle()
    rig.audio.say(case['opening'], cast(Lang, case['lang']))
    await until(lambda: len(jev.snapshots) == 1)
    jev.answer(ready=case['ready_during_gap'])
    await rig.advance(case['gap_s'])
    await until(lambda: len(jev.snapshots) == 2)
    assert not talker_got(rig)
    jev.answer(ready=case['ready_during_gap'])
    await rig.settle()
    rig.audio.say(case['continuation'], cast(Lang, case['lang']))
    await until(lambda: len(jev.snapshots) == 3)
    snapshot = jev.snapshots[-1]
    assert snapshot.sources[0].text == case['opening']
    assert snapshot.sources[1].text == case['continuation']
    assert snapshot.sources[1].started_at - snapshot.sources[0].speech_at == pytest.approx(case['gap_s'])
    assert snapshot.pauses[-1] == (snapshot.sources[0].speech_at, snapshot.sources[1].started_at)
    jev.answer(ready=True)
    await until(lambda: len(talker_got(rig)) == 1)
    assert rig.user_messages() == [f'<user lang="{case["lang"]}">{case["expected"]}</user>']
    await rig.advance(.5)
    assert len(talker_got(rig)) == 1


async def test_unfinished_thinking_continuation_needs_readiness_but_no_floor_label(rig_for: MakeRig) -> None:
    from tests.fakes import AwaitInterrupt, ResolveScope

    jev = ScriptedJev()
    rig = await rig_for([AwaitInterrupt()], [ResolveScope('preserve_tasks'), Text('I will explain the tests.')], recognition=jev)
    rig.audio.wake()
    rig.audio.say('Explain the build')
    await until(lambda: len(talker_got(rig)) == 1)
    assert rig.state == 'thinking'
    rig.audio.say('including the tests,')
    await until(lambda: len(jev.snapshots) == 1)
    jev.answer(ready=False)
    await rig.advance(2)
    await until(lambda: len(jev.snapshots) == 2)
    assert len(talker_got(rig)) == 1 and 'stop' not in rig.player.events
    assert jev.snapshots[-1].preceding_request == 'Explain the build'
    jev.answer(ready=False)
    await rig.settle()
    rig.audio.say('especially the skipped retry case.')
    await until(lambda: len(jev.snapshots) == 3)
    jev.answer(floor=False, ready=True)
    await until(lambda: len(talker_got(rig)) == 2)
    assert rig.user_messages()[-1] == '<user lang="en">Explain the build including the tests, especially the skipped retry case.</user>'
    assert rig.player.events.count('stop') == 1


async def test_initial_retention_limit_reports_recovery_instead_of_submitting_a_truncated_thought(rig_for: MakeRig) -> None:
    jev = ScriptedJev(auto_initial=False)
    rig = await rig_for([Text('Must not run.')], recognition=jev)
    rig.audio.wake()
    await rig.settle()
    rig.audio.say('The build passes, but')
    await until(lambda: len(jev.snapshots) == 1)
    jev.answer(ready=False)
    await rig.settle()
    await rig.advance(31)
    assert any('retention limit' in notice for notice in rig.output.notices)
    assert not talker_got(rig)
    assert rig.state == 'idle'


@pytest.mark.parametrize('case', CASES['finished'], ids=lambda case: case['id'])
async def test_finished_controls_report_end_wait_separately_from_meaningful_audio(rig_for: MakeRig, case: dict[str, Any]) -> None:
    jev = ScriptedJev(auto_initial=False)
    rig = await rig_for([Text('Understood.')], recognition=jev)
    rig.audio.wake()
    await rig.settle()
    rig.audio.speech()
    source, owner, speech_end = rig.audio.candidate, rig.audio.owner, rig.clock.now()
    await rig.advance(case['endpoint_delay_s'])
    rig.audio.emit(Heard(case['text'], cast(Lang, case['lang']), speech_end, owner, source, speech_end))
    await until(lambda: len(jev.snapshots) == 1)
    jev.answer(ready=True)
    await until(lambda: len(talker_got(rig)) == 1 and rig.state == 'follow_up')
    [readiness] = [row for row in rig.metrics.records if row['kind'] == 'cumulative_readiness']
    assert readiness['end_wait_ms'] == 500
    assert readiness['readiness_wait_ms'] == 0
    assert 'first_meaningful_audio_ms' not in readiness
    replies = [row for row in rig.metrics.records if row['kind'] == 'speech' and row.get('request_id') == readiness['request_id']]
    assert len(replies) == 1
    assert replies[0]['meaningful_audio_basis'] == 'queue_estimate'
    assert rig.user_messages() == [f'<user lang="{case["lang"]}">{case["text"]}</user>']


async def test_initial_partial_corrections_replace_hypotheses_and_only_the_final_source_is_submitted(rig_for: MakeRig) -> None:
    jev = ScriptedJev(auto_initial=False)
    rig = await rig_for([Text('Understood.')], recognition=jev)
    rig.audio.wake()
    await rig.settle()
    rig.audio.speech()
    source, owner, now = rig.audio.candidate, rig.audio.owner, rig.clock.now()
    rig.audio.emit(Hypothesis('Open a PR', now, owner, source, 1, now, now))
    await until(lambda: len(jev.snapshots) == 1)
    jev.answer(ready=True)
    await rig.settle()
    assert not talker_got(rig)
    rig.audio.emit(Hypothesis('Explain the PR', now, owner, source, 2, now, now))
    await until(lambda: len(jev.snapshots) == 2)
    assert len(jev.snapshots[-1].sources) == 1
    assert jev.snapshots[-1].sources[0].text == 'Explain the PR'
    jev.answer(ready=True)
    await rig.settle()
    assert not talker_got(rig)
    final = Heard('Explain the issue.', 'en', now, owner, source, now)
    rig.audio.emit(final)
    await until(lambda: len(jev.snapshots) == 3)
    jev.answer(ready=True)
    await until(lambda: len(talker_got(rig)) == 1)
    assert rig.user_messages() == ['<user lang="en">Explain the issue.</user>']
    rig.audio.emit(final)
    await rig.settle()
    assert len(talker_got(rig)) == 1


async def test_typed_replacement_retires_an_already_observed_initial_thought(rig_for: MakeRig) -> None:
    jev = ScriptedJev(auto_initial=False)
    rig = await rig_for([Text('Typed answer.')], [Text('Must not run.')], recognition=jev)
    rig.audio.wake()
    await rig.settle()
    rig.audio.say('Explain the old spoken request.')
    await until(lambda: len(jev.snapshots) == 1)
    rig.typist.type('Use this typed request instead')
    await until(lambda: 'Typed answer.' in rig.output.replies)
    jev.answer(ready=True)
    await rig.settle()
    assert len(talker_got(rig)) == 1
    assert rig.user_messages() == ['<user lang="en" typed="true">Use this typed request instead</user>']


@pytest.mark.parametrize('control', ["that's all", 'basta così'])
async def test_initial_whole_end_controls_finish_silently_without_a_talker_request(rig_for: MakeRig, control: str) -> None:
    jev = ScriptedJev(auto_initial=False)
    rig = await rig_for([Text('Must not run.')], recognition=jev)
    rig.audio.wake()
    await rig.settle()
    rig.audio.say(control)
    await rig.settle()
    assert rig.state == 'idle' and not talker_got(rig)
    assert not jev.snapshots
