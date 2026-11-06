"""Delayed Announcement sources retain bounded ownership without replaying delivered news."""

import asyncio

import pytest

from jarvis_hub.voice.assessment import SegmentAction
from jarvis_hub.voice.bargein import Partial
from jarvis_hub.voice.state import Heard, Overheard
from tests.fakes import AwaitInterrupt, Text, until
from tests.test_playback_admission import Decisions
from tests.test_voice_mode import AWAY, NEWS, QUIET, MakeRig, arrive, news_rig, rig_for as rig_for, talker_got


async def test_long_announcement_onset_survives_completion_and_expiry_once(rig_for: MakeRig) -> None:
    decisions = Decisions([('floor',)])
    rig, [arrival] = await news_rig(rig_for, [Text(NEWS)], [Text('The explanation, sir.')], recognition=decisions)
    rig.player.hold = True
    await arrive(rig, arrival)
    await until(lambda: rig.state == 'announce' and NEWS in rig.player.texts)
    onset, owner = rig.clock.now(), rig.audio.owner
    rig.audio.speech()
    candidate = rig.audio.candidate
    await rig.settle()
    rig.player.finish()
    await rig.advance(.1)
    await until(lambda: rig.state == 'follow_up')
    await rig.advance(5)
    assert rig.state == 'idle'
    raw = 'Before anything else, explain the original result and all its retry rules.'
    heard = Heard(raw, 'en', rig.clock.now(), owner, candidate, onset)
    rig.audio.emit(heard)
    await until(lambda: len(talker_got(rig)) == 3)
    rig.audio.emit(heard)
    await rig.settle()
    assert len(talker_got(rig)) == 3
    assert decisions.candidates[0].observed_segments == (raw,)
    assert decisions.candidates[0].phase == 'announce'
    assert decisions.candidates[0].admitted_turn == ''
    assert NEWS in decisions.candidates[0].played_text
    assert raw in talker_got(rig)[-1]


@pytest.mark.parametrize('opening_action', ['ignore', 'uncertain'])
async def test_ambiguous_announcement_opening_and_later_directed_segment_arrive_whole(rig_for: MakeRig, opening_action: SegmentAction) -> None:
    decisions = Decisions([(opening_action,), ('floor',) if opening_action == 'ignore' else ('floor', 'floor')])
    rig, [arrival] = await news_rig(rig_for, [Text(NEWS + ' '), AwaitInterrupt()], [Text('The details, sir.')], recognition=decisions)
    rig.player.hold = True
    await arrive(rig, arrival)
    await until(lambda: rig.state == 'announce' and NEWS in rig.player.texts)
    opening, correction = 'Regarding that original result', 'Jarvis, explain the retry behavior.'
    rig.audio.say(opening)
    await until(lambda: len(decisions.candidates) == 1)
    await rig.settle()
    await rig.advance(4)
    assert 'pause' not in rig.player.events and 'stop' not in rig.player.events
    rig.audio.say(correction)
    await until(lambda: len(talker_got(rig)) == 3)
    expected = (correction,) if opening_action == 'ignore' else (opening, correction)
    assert decisions.candidates[-1].observed_segments == expected
    assert f'<user lang="en">{" ".join(expected)}</user>' in talker_got(rig)[-1]
    assert rig.player.events.count('stop') == 1


async def test_queued_news_after_interrupted_announcement_is_delivered_once(rig_for: MakeRig) -> None:
    decisions = Decisions([('floor',)])
    rig, arrivals = await news_rig(rig_for, [Text(NEWS + ' '), AwaitInterrupt()], [Text('The correction, sir.')],
                                   [Text('The second update, sir.')], items=2, recognition=decisions)
    rig.player.hold = True
    await arrive(rig, arrivals[0])
    await until(lambda: rig.state == 'announce' and NEWS in rig.player.texts)
    await arrive(rig, arrivals[1])
    assert len(talker_got(rig)) == 2
    rig.audio.say('Explain the first update instead.')
    await until(lambda: 'The correction, sir.' in rig.player.texts)
    rig.player.hold = False
    rig.player.finish()
    await rig.advance(.1)
    await until(lambda: rig.state == 'follow_up')
    await rig.advance(6)
    await until(lambda: len(talker_got(rig)) == 4)
    assert 'T2' in talker_got(rig)[-1] and 'T1' not in talker_got(rig)[-1]
    await rig.advance(10)
    assert len(talker_got(rig)) == 4
    assert rig.player.texts.count(NEWS) == 1


async def test_quiet_news_count_is_not_replayed_after_voice_only_stop(rig_for: MakeRig) -> None:
    decisions = Decisions([('stop',)])
    rig, arrivals = await news_rig(rig_for, QUIET, [Text(AWAY + ' '), AwaitInterrupt()], items=2, recognition=decisions)
    rig.audio.wake()
    rig.audio.say('Quiet mode, please')
    await until(lambda: rig.state == 'follow_up')
    await rig.advance(6)
    for arrival in arrivals:
        await arrive(rig, arrival)
    rig.audio.wake()
    await until(lambda: rig.state == 'listening')
    await rig.advance(6.5)
    rig.player.hold = True
    await until(lambda: rig.state == 'announce' and AWAY in rig.player.texts)
    rig.audio.say('Please stop your voice.')
    await until(lambda: rig.state == 'follow_up')
    before = len(talker_got(rig))
    await rig.advance(12)
    assert len(talker_got(rig)) == before
    assert rig.player.texts.count(AWAY) == 1
    assert len(rig.output.shown) == 1


@pytest.mark.parametrize('replacement', ['typed', 'shutdown'])
async def test_late_announcement_decision_and_legacy_partials_cannot_cross_replacement(rig_for: MakeRig, replacement: str) -> None:
    gate = asyncio.Event()
    decisions = Decisions([('floor',)], gate)
    rig, [arrival] = await news_rig(rig_for, [Text(NEWS + ' '), AwaitInterrupt()], [Text('Typed replacement, sir.')],
                                   [Text('Must not run.')], recognition=decisions)
    rig.player.hold = True
    await arrive(rig, arrival)
    await until(lambda: rig.state == 'announce')
    rig.audio.say('Actually explain the original update.')
    await until(lambda: len(decisions.candidates) == 1)
    if replacement == 'typed':
        rig.typist.type('Show status instead')
        await until(lambda: 'Typed replacement, sir.' in rig.output.replies)
    else:
        await rig.voice.close()
    messages, events = len(talker_got(rig)), rig.player.events.copy()
    gate.set()
    now = rig.clock.now()
    rig.audio.emit(Partial('Jarvis stop', now, .3, 0))
    rig.audio.emit(Overheard('Jarvis stop', now))
    rig.player.finish()
    await rig.settle()
    assert len(talker_got(rig)) == messages
    assert rig.player.events == events
    assert not any('Actually explain' in message for message in talker_got(rig))


async def test_limited_announcement_candidate_notices_without_disrupting_output_or_admitting_tail(rig_for: MakeRig) -> None:
    decisions = Decisions([('ignore',)])
    rig, [arrival] = await news_rig(rig_for, [Text(NEWS + ' '), AwaitInterrupt()], recognition=decisions)
    rig.player.hold = True
    await arrive(rig, arrival)
    await until(lambda: NEWS in rig.player.texts and rig.state == 'announce')
    rig.audio.say('A request beyond the duration bound', limited=True)
    await rig.settle()
    assert any('duration limit' in str(entry) for entry in rig.output.log)
    assert decisions.candidates == []
    assert rig.state == 'announce' and 'stop' not in rig.player.events and 'pause' not in rig.player.events
    rig.audio.say('the leftover tail')
    await until(lambda: len(decisions.candidates) == 1)
    await rig.settle()
    assert decisions.candidates[0].observed_segments == ('the leftover tail',)
    assert len(talker_got(rig)) == 2
