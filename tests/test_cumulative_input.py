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
