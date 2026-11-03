"""Checks for the recording-session kit that need no mic, no speakers and no models.

    uv run test_session.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

SCRATCH = Path(tempfile.mkdtemp())
os.environ["JARVIS_AUDIO_DATA"] = str(SCRATCH / "data")
os.environ["JARVIS_AUDIO_RESULTS"] = str(SCRATCH / "results")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import report  # noqa: E402
import session  # noqa: E402

SR = common.SR


def tone(freq: float, seconds: float, sr: int) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_recording_results_keep_transcripts_out_of_the_tracked_folder():
    stt = {"source": "recordings", "wer_pct": {"all": 3.1},
           "partials": {"0.4s": {"ms_p50": 80.0, "samples": ["en01: 'what are' <- 'What are you'"]}},
           "clips": {"en01": {"ms_p50": 90.0, "ref": "What are you working on?", "hyp": "what are you working on"}}}
    tracked = common.write_result("stt-x-recordings", stt)
    kept = json.loads(tracked.read_text())
    assert tracked.parent == SCRATCH / "results"
    assert "what are you working on" not in tracked.read_text() and "what are'" not in tracked.read_text()
    assert kept["wer_pct"] == {"all": 3.1} and kept["clips"]["en01"]["ms_p50"] == 90.0
    full = json.loads((SCRATCH / "data" / "results" / "stt-x-recordings.json").read_text())
    assert full["clips"]["en01"]["hyp"] == "what are you working on"


def test_lang_results_on_recordings_drop_the_misheard_text_but_keep_ids():
    lang = {"source": "recordings", "engines": {"p": {"correct": "29/30", "wrong_ids": ["it04"], "wrong": ["it04: 'la ci' -> en"]}}}
    kept = json.loads(common.write_result("lang-recordings", lang).read_text())
    assert kept["engines"]["p"] == {"correct": "29/30", "wrong_ids": ["it04"]}


def test_synthetic_results_are_written_whole():
    data = {"source": "synthetic", "clips": {"en01": {"hyp": "what are you working on"}}}
    assert json.loads(common.write_result("stt-x-synthetic", data).read_text()) == data


def test_tracked_results_name_no_home_folder():
    tracked = subprocess.run(["git", "ls-files", "spikes/*/results/*"], cwd=common.HERE.parent.parent,
                             capture_output=True, text=True, check=True).stdout.split()
    assert tracked
    for name in tracked:
        text = (common.HERE.parent.parent / name).read_text()
        assert "/Users/" not in text and str(Path.home()) not in text, name


def test_written_results_give_paths_relative_to_the_data_folder():
    run = common.DATA / "recordings" / "speaker-run"
    data = {"run": str(run), "log": str(Path.home() / "elsewhere" / "x.log"), "files": [str(run / "mic.wav")]}
    text = common.write_result("paths-synthetic", data).read_text()
    assert str(Path.home()) not in text and "/Users/" not in text
    kept = json.loads(text)
    assert kept["run"] == "recordings/speaker-run" and kept["files"] == ["recordings/speaker-run/mic.wav"]
    assert kept["log"] == "~/elsewhere/x.log"


def test_resample_keeps_a_tone_and_rejects_what_the_target_rate_cannot_hold():
    out = common.resample(tone(1000, 1.0, 48_000) + tone(12_000, 1.0, 48_000), 48_000, SR)
    assert len(out) == SR
    spectrum = np.abs(np.fft.rfft(out))
    # 12 kHz is above 16 kHz's 8 kHz Nyquist: unfiltered, it would alias to 4 kHz, so 4 kHz must stay near silent.
    assert spectrum[4000] < spectrum[1000] / 100
    assert spectrum[1000] > 1000


def test_find_pause_locates_a_one_second_gap_and_ignores_word_gaps():
    rng = np.random.default_rng(0)
    room = lambda s: rng.normal(0, 0.002, int(s * SR)).astype(np.float32)  # noqa: E731
    paused = np.concatenate([room(0.3), tone(200, 1.2, SR), room(1.0), tone(200, 1.0, SR), room(0.3)])
    start, end = common.find_pause(paused)
    assert abs(start - 1.5) < 0.05 and abs(end - 2.5) < 0.05
    fluent = np.concatenate([room(0.3), tone(200, 0.8, SR), room(0.15), tone(200, 0.8, SR), room(0.3)])
    assert common.find_pause(fluent) is None


def test_barge_in_cues_and_their_windows_fall_inside_the_recording():
    # Jarvis's lines in the synthetic speaker run last 29 s per half.
    ref_s = 2 * 29.0 + session.SPEAKER_TAIL_S
    cues = session.cue_times(ref_s)
    assert len(cues) == len(session.BARGE_IN_WORDS)
    assert cues[0] > 29.0
    assert cues[-1] + session.USER_SPAN_S <= ref_s - 0.5
    assert all(b - a >= session.USER_SPAN_S for a, b in zip(cues, cues[1:]))


def test_cue_times_refuse_a_reference_too_short_for_five_cues():
    try:
        session.cue_times(2 * 10.0 + session.SPEAKER_TAIL_S)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_vpio_marks_are_shifted_by_when_playback_began_in_the_recording():
    out = session.parse_helper_output("recording_started_at=1000.25\nplaying_at=1000.75\nwrote x.wav\n")
    m = session.marks([30.0, 36.0], out["playing_at"] - out["recording_started_at"])
    assert m["jarvis_only"] == [0.5, 30.5]
    assert m["user_speech"] == [[30.5, 30.5 + session.USER_SPAN_S], [36.5, 36.5 + session.USER_SPAN_S]]


def test_a_failed_or_empty_vpio_run_is_not_scored():
    wav = SCRATCH / "vpio.wav"
    times = {"recording_started_at": 1.0, "playing_at": 1.2}
    common.save_16k(wav, np.zeros(0, np.float32))
    assert session.vpio_failure(1, times, wav) == "helper exited with 1"
    assert session.vpio_failure(0, times, wav) == "output shorter than a second"
    common.save_16k(wav, np.zeros(SR * 5, np.float32))
    assert "silent" in session.vpio_failure(0, times, wav)
    assert "never reported" in session.vpio_failure(0, {"playing_at": 1.2}, wav)
    common.save_16k(wav, tone(300, 5, SR))
    assert session.vpio_failure(0, times, wav) is None


def test_enter_click_is_trimmed_from_the_end_of_a_take():
    assert len(session.drop_enter_click(np.ones(SR, np.float32))) == SR - int(0.15 * SR)


def test_report_replaces_its_block_instead_of_adding_another():
    notes = "# Notes\n\n## Audio\n\nText.\n\n## Next\n\nMore.\n"
    once = report.place(notes, "### Results (day 1)\n\nold")
    twice = report.place(once, "### Results (day 2)\n\nnew")
    assert twice.count(report.START) == 1 and "old" not in twice and "new" in twice
    assert twice.index("new") < twice.index("## Next") and twice.index("Text.") < twice.index("new")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
