"""Cumulative source ownership through playback, Follow-up, news and delayed control."""

import pytest

from jarvis_hub.voice.language import Lang
from jarvis_hub.voice.state import Heard, Hypothesis
from tests.fakes import ResolveScope, Text, until
from tests.test_cumulative_voice import ScriptedJev
from tests.test_voice_mode import MakeRig, rig_for as rig_for, talker_got


async def test_unresolved_cumulative_thought_survives_follow_up_expiry_and_retains_its_opening(rig_for: MakeRig) -> None:
    jev = ScriptedJev()
    rig = await rig_for([Text('The build passed.')], [ResolveScope('preserve_tasks'), Text('Explained.')], recognition=jev)
    rig.audio.wake()
    rig.audio.say('Explain the build')
    await until(lambda: rig.state == 'follow_up')
    rig.audio.say('Actually, include the skipped test,')
    await until(lambda: len(jev.snapshots) == 1)
    jev.answer(ready=False)
    await rig.advance(7)
    await until(lambda: len(jev.snapshots) == 2)
    jev.answer(ready=False)
    await rig.settle()
    assert rig.audio.capturing[-1] is True
    rig.audio.say('especially its retry behavior.')
    await until(lambda: len(jev.snapshots) == 3)
    assert tuple(source.text for source in jev.snapshots[-1].sources) == ('Actually, include the skipped test,', 'especially its retry behavior.')
    assert jev.snapshots[-1].preceding_request == 'Explain the build'
    jev.answer(ready=True)
    await until(lambda: len(talker_got(rig)) == 2)
    assert 'Actually, include the skipped test, especially its retry behavior.' in talker_got(rig)[-1]


async def test_new_correction_during_blocked_interrupt_prevents_obsolete_ready_submission(rig_for: MakeRig) -> None:
    import asyncio
    from tests.fakes import AwaitInterrupt
    from tests.test_voice_mode import STORY, speaking

    interrupted = asyncio.Event()
    jev = ScriptedJev()
    rig = await rig_for([*STORY, AwaitInterrupt()], [ResolveScope('preserve_tasks'), Text('Latest correction.')],
                        recognition=jev, interrupt_gate=interrupted)
    await speaking(rig)
    rig.audio.say('Explain repository A.')
    await until(lambda: len(jev.snapshots) == 1)
    jev.answer(floor=True, ready=True)
    await until(lambda: 'stop' in rig.player.events)
    rig.audio.say('Actually, use repository B.')
    await rig.settle()
    interrupted.set()
    await until(lambda: len(jev.snapshots) == 2)
    assert len(talker_got(rig)) == 1
    assert tuple(source.text for source in jev.snapshots[-1].sources) == ('Explain repository A.', 'Actually, use repository B.')
    assert jev.snapshots[-1].request is not None and jev.snapshots[-1].request[1] == 1
    jev.answer(ready=True)
    await until(lambda: len(talker_got(rig)) == 2)
    assert 'Actually, use repository B.' in talker_got(rig)[-1]
    assert 'revision="2"' in talker_got(rig)[-1]
    assert rig.player.events.count('stop') == 1


async def test_twelve_segment_explanation_with_repeated_pauses_retains_every_word_once(rig_for: MakeRig) -> None:
    jev = ScriptedJev(auto_initial=False)
    rig = await rig_for([Text('All twelve points are covered.')], recognition=jev)
    rig.audio.wake()
    await rig.settle()
    segments = ('Explain the build,', 'including the first retry,', 'the second retry,', 'the cache state,',
                'the skipped test,', 'the worker log,', 'the queue depth,', 'the changed branch,',
                'the failed hook,', 'the original request,', 'the pending approval,', 'and the final result.')
    for index, text in enumerate(segments):
        before = len(jev.snapshots)
        rig.audio.say(text)
        await until(lambda: len(jev.snapshots) == before + 1)
        assert tuple(source.text for source in jev.snapshots[-1].sources) == segments[:index + 1]
        jev.answer(ready=index == len(segments) - 1)
        await rig.settle()
        if index < len(segments) - 1:
            before = len(jev.snapshots)
            await rig.advance(3)
            await until(lambda: len(jev.snapshots) == before + 1)
            jev.answer(ready=False)
            await rig.settle()
            assert not talker_got(rig)
    await until(lambda: len(talker_got(rig)) == 1)
    assert rig.user_messages() == ['<user lang="en">Explain the build, including the first retry, the second retry, the cache state, the skipped test, the worker log, the queue depth, the changed branch, the failed hook, the original request, the pending approval, and the final result.</user>']
    assert not any('retention limit' in notice for notice in rig.output.notices)


async def test_late_eligible_follow_up_onset_is_assessed_after_window_expiry(rig_for: MakeRig) -> None:
    jev = ScriptedJev()
    rig = await rig_for([Text('The build passed.')], [ResolveScope('preserve_tasks'), Text('Explained.')], recognition=jev)
    rig.audio.wake()
    rig.audio.say('Explain the build')
    await until(lambda: rig.state == 'follow_up')
    onset, owner, source = rig.clock.now() + 1, rig.audio.owner, rig.audio.candidate + 1
    await rig.advance(7)
    assert rig.state == 'idle'
    rig.audio.emit(Heard('Include its retry behavior.', 'en', rig.clock.now(), owner, source, onset))
    await until(lambda: len(jev.snapshots) == 1)
    assert jev.snapshots[0].phase == 'follow_up'
    assert jev.snapshots[0].preceding_request == 'Explain the build'
    jev.answer(ready=True)
    await until(lambda: len(talker_got(rig)) == 2)
    assert 'Include its retry behavior.' in talker_got(rig)[-1]


@pytest.mark.parametrize(('raw', 'background', 'suffix', 'expected', 'lang'), [
    ('Jarvis, explain the build. Lena, close the door. Then include retries.', 'Lena', 'Then',
     'Jarvis, explain the build. Then include retries.', 'en'),
    ('Jarvis, spiegami la build. Luca, chiudi la porta. Poi includi i tentativi.', 'Luca', 'Poi',
     'Jarvis, spiegami la build. Poi includi i tentativi.', 'it'),
])
async def test_mixed_source_selects_only_grounded_directed_word_spans(
    rig_for: MakeRig, raw: str, background: str, suffix: str, expected: str, lang: Lang,
) -> None:
    from jarvis_hub.voice.cumulative import Decision, Selection
    from tests.fakes import AwaitInterrupt
    from tests.test_voice_mode import STORY, speaking

    jev = ScriptedJev()
    rig = await rig_for([*STORY, AwaitInterrupt()], [ResolveScope('preserve_tasks'), Text('Explained.')], recognition=jev)
    await speaking(rig)
    rig.audio.say(raw, lang)
    await until(lambda: len(jev.snapshots) == 1)
    snapshot = jev.snapshots[-1]
    source = snapshot.sources[0]
    selections = (Selection(source.candidate, source.revision, 0, raw.index(' ' + background)),
                  Selection(source.candidate, source.revision, raw.index(suffix), len(raw)))
    jev.replies.put_nowait(Decision(snapshot.id, (), True, True, False, 'assessed', spans=selections))
    await until(lambda: len(talker_got(rig)) == 2)
    assert expected in talker_got(rig)[-1]
    assert background not in talker_got(rig)[-1]
    assert f'<user lang="{lang}">' in talker_got(rig)[-1]


async def test_announcement_voice_only_stop_does_not_leave_an_infinite_follow_up_lease(rig_for: MakeRig) -> None:
    from tests.fakes import AwaitInterrupt
    from tests.test_voice_mode import NEWS, arrive, news_rig

    jev = ScriptedJev()
    rig, [arrival] = await news_rig(rig_for, [Text(NEWS + ' '), AwaitInterrupt()], recognition=jev)
    rig.player.hold = True
    await arrive(rig, arrival)
    await until(lambda: rig.state == 'announce' and NEWS in rig.player.texts)
    rig.audio.say('Please stop your voice.')
    await until(lambda: len(jev.snapshots) == 1)
    jev.answer(floor=True, voice_only=True)
    await until(lambda: rig.state == 'follow_up' and rig.player.pending() == 0)
    await rig.advance(7)
    assert rig.state == 'idle'
    rig.audio.say('Speech that began after the window expired.')
    await rig.settle()
    assert len(jev.snapshots) == 1
    assert len(talker_got(rig)) == 2


async def test_thinking_period_keeps_identity_across_first_reply_audio(rig_for: MakeRig) -> None:
    import asyncio
    from tests.fakes import AwaitInterrupt, Gate

    begin = asyncio.Event()
    jev = ScriptedJev()
    rig = await rig_for([Gate(begin), Text('The original reply. '), AwaitInterrupt()],
                        [ResolveScope('preserve_tasks'), Text('Corrected.')], recognition=jev)
    rig.audio.wake()
    rig.audio.say('Explain the build')
    await until(lambda: len(talker_got(rig)) == 1)
    rig.audio.speech()
    now, source, owner = rig.clock.now(), rig.audio.candidate, rig.audio.owner
    rig.audio.emit(Hypothesis('Actually, include', now, owner, source, 1, now, now))
    await until(lambda: len(jev.snapshots) == 1)
    assert jev.snapshots[0].phase == 'thinking'
    rig.player.hold = True
    await rig.advance(.1)
    begin.set()
    await until(lambda: rig.state == 'speaking')
    jev.answer(floor=True)
    await until(lambda: 'stop' in rig.player.events)
    assert len(talker_got(rig)) == 1
    rig.audio.emit(Heard('Actually, include the skipped retry test.', 'en', rig.clock.now(), owner, source, now))
    await until(lambda: len(jev.snapshots) == 2)
    assert jev.snapshots[-1].phase == 'thinking'
    assert jev.snapshots[-1].sources[0].started_at == now
    jev.answer(ready=True)
    await until(lambda: len(talker_got(rig)) == 2)
    assert 'Actually, include the skipped retry test.' in talker_got(rig)[-1]


async def test_cumulative_announcement_floor_waits_for_readiness_and_queued_second_news(rig_for: MakeRig) -> None:
    from tests.fakes import AwaitInterrupt
    from tests.test_voice_mode import DOCS, NEWS, arrive, news_rig

    jev = ScriptedJev()
    rig, arrivals = await news_rig(rig_for, [Text(NEWS + ' '), AwaitInterrupt()], [Text('The correction.')],
                                   [Text(DOCS)], items=2, recognition=jev)
    rig.player.hold = True
    await arrive(rig, arrivals[0])
    await until(lambda: rig.state == 'announce' and NEWS in rig.player.texts)
    rig.audio.speech()
    now, source, owner = rig.clock.now(), rig.audio.candidate, rig.audio.owner
    rig.audio.emit(Hypothesis('Actually, explain', now, owner, source, 1, now, now))
    await until(lambda: len(jev.snapshots) == 1)
    jev.answer(floor=True)
    await until(lambda: 'stop' in rig.player.events)
    assert len(talker_got(rig)) == 2
    await arrive(rig, arrivals[1])
    await rig.advance(7)
    await until(lambda: len(jev.snapshots) == 2)
    assert len(talker_got(rig)) == 2 and DOCS not in rig.player.texts
    assert rig.audio.capturing[-1] is True
    jev.answer(ready=False)
    await rig.settle()
    rig.player.hold = False
    rig.audio.emit(Heard('Actually, explain the original update.', 'en', rig.clock.now(), owner, source, now))
    await until(lambda: len(jev.snapshots) == 3)
    assert jev.snapshots[-1].phase == 'announce' and jev.snapshots[-1].preceding_request == ''
    jev.answer(ready=True)
    await until(lambda: rig.state == 'follow_up' and 'The correction.' in rig.player.texts)
    await rig.advance(6)
    await until(lambda: DOCS in rig.player.texts)
    assert rig.player.texts.count(NEWS) == 1 and rig.player.texts.count(DOCS) == 1
    assert 'T2' in talker_got(rig)[-1] and 'T1' not in talker_got(rig)[-1]


async def test_quiet_bundle_is_not_replayed_after_a_cumulative_voice_only_stop(rig_for: MakeRig) -> None:
    from tests.fakes import AwaitInterrupt
    from tests.test_voice_mode import AWAY, QUIET, arrive, news_rig

    jev = ScriptedJev()
    rig, arrivals = await news_rig(rig_for, QUIET, [Text(AWAY + ' '), AwaitInterrupt()], items=2, recognition=jev)
    rig.audio.wake()
    rig.audio.say('Quiet mode, please')
    await until(lambda: rig.state == 'follow_up')
    await rig.advance(6)
    for arrival in arrivals:
        await arrive(rig, arrival)
    before = len(talker_got(rig))
    await rig.advance(10)
    assert len(talker_got(rig)) == before
    rig.audio.wake()
    await rig.advance(6.5)
    rig.player.hold = True
    await until(lambda: rig.state == 'announce' and AWAY in rig.player.texts)
    rig.audio.say('Please stop your voice.')
    await until(lambda: len(jev.snapshots) == 1)
    jev.answer(floor=True, voice_only=True)
    await until(lambda: rig.state == 'follow_up' and rig.player.pending() == 0)
    before = len(talker_got(rig))
    await rig.advance(12)
    assert len(talker_got(rig)) == before and rig.player.texts.count(AWAY) == 1
    assert len(rig.output.shown) == 1


async def test_empty_endpoint_retires_noise_without_classifier_work_or_an_orphaned_listen(rig_for: MakeRig) -> None:
    jev = ScriptedJev(auto_initial=False)
    rig = await rig_for([Text('Must not run.')], recognition=jev)
    rig.audio.wake()
    await rig.settle()
    rig.audio.say('')
    await rig.settle()
    assert not jev.snapshots and not talker_got(rig)
    await rig.advance(7)
    assert rig.state == 'idle'


async def test_announcement_end_conversation_cannot_retire_an_observed_unfinished_correction(rig_for: MakeRig) -> None:
    import asyncio
    from tests.fakes import Gate, Tool
    from tests.test_voice_mode import NEWS, arrive, news_rig

    finish = asyncio.Event()
    jev = ScriptedJev()
    rig, [arrival] = await news_rig(rig_for, [Text(NEWS + ' '), Gate(finish), Tool('end_conversation')],
                                   [Text('The explanation.')], recognition=jev)
    rig.player.hold = True
    await arrive(rig, arrival)
    await until(lambda: rig.state == 'announce' and NEWS in rig.player.texts)
    rig.audio.speech()
    source, owner, onset = rig.audio.candidate, rig.audio.owner, rig.clock.now()
    rig.audio.emit(Hypothesis('Actually, explain', onset, owner, source, 1, onset, onset))
    await until(lambda: len(jev.snapshots) == 1)
    jev.answer(ready=False)
    await rig.settle()
    finish.set()
    await rig.settle()
    rig.player.hold = False
    rig.player.finish()
    await rig.advance(.1)
    assert rig.audio.capturing[-1] is True
    rig.audio.emit(Heard('Actually, explain the original update.', 'en', rig.clock.now(), owner, source, onset))
    await until(lambda: len(jev.snapshots) == 2)
    assert jev.snapshots[-1].phase == 'announce'
    jev.answer(ready=True)
    await until(lambda: len(talker_got(rig)) == 3)
    assert 'Actually, explain the original update.' in talker_got(rig)[-1]


async def test_old_reply_farewell_cannot_retire_a_cumulative_correction(rig_for: MakeRig) -> None:
    import asyncio
    from tests.fakes import Gate, Tool

    finish = asyncio.Event()
    jev = ScriptedJev()
    rig = await rig_for([Text('Until tomorrow, sir. '), Gate(finish), Tool('end_conversation')],
                        [ResolveScope('preserve_tasks'), Text('The explanation.')], recognition=jev)
    rig.player.hold = True
    rig.audio.wake()
    rig.audio.say("We're done for today")
    await until(lambda: rig.state == 'speaking')
    rig.audio.speech()
    source, owner, onset = rig.audio.candidate, rig.audio.owner, rig.clock.now()
    rig.audio.emit(Hypothesis('Actually, one more thing', onset, owner, source, 1, onset, onset))
    await until(lambda: len(jev.snapshots) == 1)
    jev.answer(ready=False)
    await rig.settle()
    finish.set()
    await until(lambda: any(tool.name == 'end_conversation' for tool in rig.backend.session('talker').tool_results))
    rig.player.hold = False
    rig.player.finish()
    await rig.advance(.1)
    assert rig.audio.capturing[-1] is True
    rig.audio.emit(Heard('Actually, one more thing: explain the retry.', 'en', rig.clock.now(), owner, source, onset))
    await until(lambda: len(jev.snapshots) == 2)
    jev.answer(ready=True)
    await until(lambda: len(talker_got(rig)) == 2)
    assert 'Actually, one more thing: explain the retry.' in talker_got(rig)[-1]


async def test_limited_late_endpoint_rejects_its_tail_even_without_a_delivered_onset(rig_for: MakeRig) -> None:
    from tests.fakes import AwaitInterrupt
    from tests.test_voice_mode import STORY, speaking

    jev = ScriptedJev()
    rig = await rig_for([*STORY, AwaitInterrupt()], recognition=jev)
    await speaking(rig)
    source, owner, now = rig.audio.candidate + 1, rig.audio.owner, rig.clock.now()
    rig.audio.emit(Heard('An over-limit correction', 'en', now, owner, source, now, limited=True))
    await rig.settle()
    rig.audio.emit(Heard('the orphaned tail', 'en', now, owner, source, now))
    await rig.settle()
    assert not jev.snapshots and len(talker_got(rig)) == 1
    assert not any(event in ('pause', 'stop') for event in rig.player.events)


@pytest.mark.parametrize('invalid', ['revision', 'inside_word', 'overlap'])
async def test_invalid_grounded_span_cannot_stop_or_submit(rig_for: MakeRig, invalid: str) -> None:
    from jarvis_hub.voice.cumulative import Decision, Selection
    from tests.fakes import AwaitInterrupt
    from tests.test_voice_mode import STORY, speaking

    jev = ScriptedJev()
    rig = await rig_for([*STORY, AwaitInterrupt()], recognition=jev)
    await speaking(rig)
    raw = 'Jarvis, explain the build.'
    rig.audio.say(raw)
    await until(lambda: len(jev.snapshots) == 1)
    snapshot = jev.snapshots[-1]
    source = snapshot.sources[0]
    span = Selection(source.candidate, source.revision + (invalid == 'revision'), 1 if invalid == 'inside_word' else 0, len(raw))
    jev.replies.put_nowait(Decision(snapshot.id, (), True, True, False, 'assessed',
                                   spans=(span, span) if invalid == 'overlap' else (span,)))
    await rig.settle()
    assert len(talker_got(rig)) == 1 and rig.state == 'speaking'
    assert not any(event in ('pause', 'stop') for event in rig.player.events)


async def test_shutdown_retires_pending_cumulative_results_and_later_audio_events(rig_for: MakeRig) -> None:
    from tests.fakes import AwaitInterrupt
    from tests.test_voice_mode import STORY, speaking

    jev = ScriptedJev()
    rig = await rig_for([*STORY, AwaitInterrupt()], recognition=jev)
    await speaking(rig)
    rig.audio.say('Actually explain the other branch.')
    await until(lambda: len(jev.snapshots) == 1)
    await rig.voice.close()
    before = rig.player.events.copy()
    jev.answer(floor=True, ready=True)
    rig.audio.say('Another late source.')
    await rig.settle()
    assert len(talker_got(rig)) == 1 and len(jev.snapshots) == 1
    assert rig.player.events == before and rig.audio.capturing[-1] is False


async def test_unrelated_early_partial_retires_prefix_but_preserves_a_later_directed_opening(rig_for: MakeRig) -> None:
    from jarvis_hub.voice.cumulative import Decision
    from tests.fakes import AwaitInterrupt
    from tests.test_voice_mode import STORY, speaking

    jev = ScriptedJev()
    rig = await rig_for([*STORY, AwaitInterrupt()], [ResolveScope('preserve_tasks'), Text('Explained.')], recognition=jev)
    await speaking(rig)
    rig.audio.speech()
    source, owner, onset = rig.audio.candidate, rig.audio.owner, rig.clock.now()
    prefix = 'Lena, close the door'
    rig.audio.emit(Hypothesis(prefix, onset, owner, source, 1, onset, onset))
    await until(lambda: len(jev.snapshots) == 1)
    first = jev.snapshots[-1]
    jev.replies.put_nowait(Decision(first.id, (), False, False, False, 'assessed', (first.sources[0].key,)))
    await rig.settle()
    assert not any(event in ('pause', 'stop') for event in rig.player.events)
    raw = 'Lena close the door. Jarvis, wait, explain the retry.'
    await rig.advance(.4)
    rig.audio.emit(Hypothesis(raw, rig.clock.now(), owner, source, 2, onset, rig.clock.now()))
    await until(lambda: len(jev.snapshots) == 2)
    jev.answer(floor=True)
    await until(lambda: 'stop' in rig.player.events)
    assert len(talker_got(rig)) == 1
    rig.audio.emit(Heard(raw, 'en', rig.clock.now(), owner, source, onset))
    await until(lambda: len(jev.snapshots) == 3)
    jev.answer(ready=True)
    await until(lambda: len(talker_got(rig)) == 2)
    assert 'Jarvis, wait, explain the retry.' in talker_got(rig)[-1]
    assert 'Lena' not in talker_got(rig)[-1] and 'close the door' not in talker_got(rig)[-1]


async def test_incompatible_retired_prefix_rewrite_is_visible_and_cannot_admit_an_orphan_tail(rig_for: MakeRig) -> None:
    from jarvis_hub.voice.cumulative import Decision
    from tests.fakes import AwaitInterrupt
    from tests.test_voice_mode import STORY, speaking

    jev = ScriptedJev()
    rig = await rig_for([*STORY, AwaitInterrupt()], [ResolveScope('preserve_tasks'), Text('Explained.')], recognition=jev)
    await speaking(rig)
    rig.audio.speech()
    source, owner, now = rig.audio.candidate, rig.audio.owner, rig.clock.now()
    rig.audio.emit(Hypothesis('Lena close the door', now, owner, source, 1, now, now))
    await until(lambda: len(jev.snapshots) == 1)
    first = jev.snapshots[-1]
    jev.replies.put_nowait(Decision(first.id, (), False, False, False, 'assessed', (first.sources[0].key,)))
    await rig.settle()
    rig.audio.emit(Hypothesis('An incompatible rewrite Jarvis explain the build', now, owner, source, 2, now, now))
    await rig.settle()
    assert any('retired background prefix' in notice for notice in rig.output.notices)
    rig.audio.emit(Heard('Jarvis explain the orphan tail', 'en', now, owner, source, now))
    await rig.settle()
    assert len(jev.snapshots) == 1 and len(talker_got(rig)) == 1
    rig.audio.say('Jarvis, explain the retry.')
    await until(lambda: len(jev.snapshots) == 2)
    jev.answer(floor=True, ready=True)
    await until(lambda: len(talker_got(rig)) == 2)
    assert 'Jarvis, explain the retry.' in talker_got(rig)[-1]
    assert 'orphan' not in talker_got(rig)[-1]
