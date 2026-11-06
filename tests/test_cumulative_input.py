"""Cumulative capture uses revised source hypotheses before the acoustic endpoint."""

from jarvis_hub.voice.state import Heard, Hypothesis
from tests.test_input_pipeline import rig


def test_capture_exposes_replacement_hypotheses_before_endpoint_with_source_and_pause_timing() -> None:
    r = rig(cumulative_stt=True)
    r.pipeline.capture(True, owner=7)
    r.stt.text = 'No wait'
    r.feed('speech', .45)
    first = next(event for event in r.events if isinstance(event, Hypothesis))
    assert first.text == 'No wait' and first.owner == 7 and first.revision == 1
    assert not any(isinstance(event, Heard) for event in r.events)
    r.stt.text = 'No, wait, I meant'
    r.feed('speech', .45)
    r.feed('silence', .25)
    hypotheses = [event for event in r.events if isinstance(event, Hypothesis)]
    assert hypotheses[-1].text == 'No, wait, I meant'
    assert hypotheses[-1].candidate == first.candidate
    assert hypotheses[-1].revision > first.revision
    assert hypotheses[-1].speech_at <= hypotheses[-1].at
    rows = [row for row in r.records if row['kind'] == 'cumulative_stt']
    assert rows and all(row['mechanism'] == 'repeated_batch' for row in rows)
    assert all(isinstance(row['stt_ms'], (int, float)) and row['stt_ms'] >= 0 for row in rows)


def test_cumulative_capture_replay_replaces_a_resumed_endpoint_before_final_source_delivery() -> None:
    from jarvis_hub.voice.input import Engines, InputPipeline, Listening
    from tests.test_input_pipeline import Everyone, LoudVad, MarkerWake, PassThrough, Rig, ScriptedStt, ScriptedTurn

    echo, turn, stt = PassThrough(), ScriptedTurn(), ScriptedStt(text='No, wait')
    pipeline = InputPipeline(Engines(echo, MarkerWake(), LoudVad(), turn, stt, Everyone()),
                             Listening(cumulative_stt=True), record=lambda row: None, defer_endpoints=True)
    r = Rig(pipeline, turn, stt, echo)
    pipeline.capture(True, owner=5)
    r.feed('speech', .6)
    r.feed('silence', .55)
    first_endpoint = pipeline.pending_endpoint
    assert first_endpoint is not None
    assert not r.heard()
    first_hypothesis = next(event for event in r.events if isinstance(event, Hypothesis))
    stt.text = 'No, wait, I meant the other branch.'
    r.feed('speech', .3)
    assert pipeline.finalize_endpoint(first_endpoint) is None
    r.feed('speech', .3)
    r.feed('silence', .55)
    latest_endpoint = pipeline.pending_endpoint
    assert latest_endpoint is not None and latest_endpoint != first_endpoint
    final = pipeline.finalize_endpoint(latest_endpoint)
    assert final is not None and final.text == 'No, wait, I meant the other branch.'
    assert final.candidate == first_hypothesis.candidate and final.owner == 5
    assert pipeline.finalize_endpoint(latest_endpoint) is None
    hypotheses = [event for event in r.events if isinstance(event, Hypothesis)]
    assert hypotheses[-1].text == 'No, wait, I meant the other branch.'
    assert hypotheses[-1].revision > first_hypothesis.revision
