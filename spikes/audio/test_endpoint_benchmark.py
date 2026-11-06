import numpy as np

from bench_endpoint import ComparedTurn, replay
from jarvis_hub.voice.input import Engines, Listening
from tests.test_input_pipeline import Everyone, LoudVad, MarkerWake, PassThrough, ScriptedStt, ScriptedTurn


def test_replay_counts_split_requests_and_preserved_continuations() -> None:
    audio = np.concatenate([
        np.zeros(1600), np.full(9600, 1000), np.zeros(19200),
        np.full(9600, 3000), np.zeros(48000),
    ]).astype(np.int16)

    def engines(verdicts: list[float]) -> Engines:
        return Engines(
            PassThrough(), MarkerWake(), LoudVad(), ScriptedTurn(verdicts),
            ScriptedStt(words={1000: 'Open an issue', 3000: 'about retry failures'}), Everyone(),
        )

    cut = replay(audio, engines([]), Listening())
    held = replay(audio, engines([0.01, 0.99]), Listening())

    assert cut['premature_commit'] is True
    assert cut['requests'] == 2
    assert held['premature_commit'] is False
    assert held['requests'] == 1
    assert held['completed_turns'] == 1
    assert held['final_state'] == 'follow_up'


def test_empty_and_quiet_input_do_not_become_requests() -> None:
    for audio in (np.zeros(0, np.int16), np.full(16000, 3, np.int16)):
        engines = Engines(PassThrough(), MarkerWake(), LoudVad(), ScriptedTurn(), ScriptedStt(), Everyone())
        result = replay(audio, engines, Listening())
        assert result['requests'] == 0
        assert result['completed_turns'] == 0
        assert result['premature_commit'] is False


def test_wait_percentiles_use_the_declared_method_and_keep_missing_samples_visible() -> None:
    from bench_endpoint import distribution

    assert distribution(list(range(10))) == {'n': 10, 'p50': 4.5, 'p90': 8}
    assert distribution([]) == {'n': 0, 'p50': None, 'p90': None}


def test_replay_expires_follow_up_before_unaddressed_speech_after_a_long_gap() -> None:
    audio = np.concatenate([
        np.zeros(1600), np.full(9600, 1000), np.zeros(160000),
        np.full(9600, 3000), np.zeros(48000),
    ]).astype(np.int16)
    engines = Engines(
        PassThrough(), MarkerWake(), LoudVad(), ScriptedTurn(),
        ScriptedStt(words={1000: 'Open an issue', 3000: 'Unaddressed later speech'}), Everyone(),
    )

    result = replay(audio, engines, Listening())

    assert result['requests'] == 1
    assert result['completed_turns'] == 1
    assert result['final_state'] == 'idle'


def test_speech_ready_on_the_follow_up_deadline_keeps_its_continuation() -> None:
    class FrameVad(LoudVad):
        chunk = 160

    audio = np.concatenate([
        np.zeros(1600), np.full(9600, 1000), np.zeros(110240),
        np.full(16000, 3000), np.zeros(48000),
    ]).astype(np.int16)
    engines = Engines(
        PassThrough(), MarkerWake(), FrameVad(), ScriptedTurn(),
        ScriptedStt(words={1000: 'Open an issue', 3000: 'Another complete request'}), Everyone(),
    )

    result = replay(audio, engines, Listening())

    assert result['requests'] == 2
    assert result['completed_turns'] == 2
    assert result['final_state'] == 'follow_up'


def test_replay_distinguishes_silence_fallback_from_a_complete_score() -> None:
    audio = np.concatenate([np.full(9600, 1000), np.zeros(48000)]).astype(np.int16)
    engines = Engines(
        PassThrough(), MarkerWake(), LoudVad(), ScriptedTurn([0.9869]), ScriptedStt(), Everyone(),
    )

    result = replay(audio, engines, Listening(smart_turn_threshold=.987))

    assert result['requests'] == 1
    assert result['commit_causes'] == ['silence_fallback']
    fixed = Engines(
        PassThrough(), MarkerWake(), LoudVad(), ComparedTurn(None, None, None, 'fixed-1500'),
        ScriptedStt(), Everyone(),
    )
    assert replay(audio, fixed, Listening(end_silence_s=1.5, end_extend_s=0.))['commit_causes'] == ['fixed_silence']
