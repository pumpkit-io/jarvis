"""Checks for the recording-session kit that need no mic, no speakers and no models.

    uv run test_session.py
"""

import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

SCRATCH = Path(tempfile.mkdtemp())
os.environ["JARVIS_AUDIO_DATA"] = str(SCRATCH / "data")
os.environ["JARVIS_AUDIO_RESULTS"] = str(SCRATCH / "results")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_aec  # noqa: E402
import bench_smart_turn  # noqa: E402
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
    m = session.marks([30.0, 36.0], out["playing_at"] - out["recording_started_at"], half_s=29.0)
    assert m["jarvis_only"] == [0.5, 30.5]
    assert m["echo_shift_s"] == 29.0
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


def test_a_vpio_pass_without_its_marks_is_not_done():
    run = SCRATCH / "vpio-run"
    common.save_16k(run / "vpio.wav", tone(300, 2, SR))
    assert not session.vpio_done(run, {})
    assert not session.vpio_done(run, {"vpio": {"failed": "helper timed out"}})
    assert session.vpio_done(run, {"vpio": session.marks([30.0], 0.4, half_s=29.0)})
    (run / "vpio.wav").unlink()
    assert not session.vpio_done(run, {"vpio": session.marks([30.0], 0.4, half_s=29.0)})


def test_the_room_run_resumes_with_the_chunks_still_missing():
    folder = SCRATCH / "room"
    n = session.ROOM_MINUTES // session.ROOM_CHUNK_MINUTES
    assert session.room_chunks_todo(folder, redo=False) == list(range(1, n + 1))
    for i in (1, 2, 4):
        common.save_16k(folder / f"room_{i:02d}.wav", tone(300, 1, SR))
    assert session.room_chunks_todo(folder, redo=False) == [3] + list(range(5, n + 1))
    assert session.room_chunks_todo(folder, redo=True) == list(range(1, n + 1))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve(status: int) -> tuple[http.server.HTTPServer, int]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = http.server.HTTPServer(("127.0.0.1", free_port()), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def test_a_server_is_ready_only_when_it_answers_with_success():
    assert not session.server_ready(free_port())
    for status, ready in ((503, False), (200, True)):
        server, port = serve(status)
        try:
            assert session.server_ready(port) is ready
        finally:
            server.shutdown()


def stub_server(body: str) -> Path:
    stub = SCRATCH / "whisper-server-stub"
    stub.write_text(f"#!/bin/sh\necho $$ > {SCRATCH / 'stub.pid'}\n{body}\n")
    stub.chmod(0o755)
    return stub


def test_turbo_server_that_crashes_or_never_answers_gives_no_row_and_is_stopped():
    model = SCRATCH / "model.bin"
    model.write_bytes(b"")
    saved = session.WHISPER_SERVER, session.TURBO_MODEL, session.TURBO_PORT, session.SERVER_READY_TIMEOUT_S
    try:
        session.TURBO_MODEL, session.TURBO_PORT, session.SERVER_READY_TIMEOUT_S = model, free_port(), 2
        session.WHISPER_SERVER = stub_server("exit 3")
        assert session.start_turbo_server() is None
        session.WHISPER_SERVER = stub_server("exec sleep 60")
        assert session.start_turbo_server() is None
        pid = int((SCRATCH / "stub.pid").read_text())
        time.sleep(0.2)
        assert subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode != 0, "stub still running"
    finally:
        session.WHISPER_SERVER, session.TURBO_MODEL, session.TURBO_PORT, session.SERVER_READY_TIMEOUT_S = saved


def test_enter_click_is_trimmed_from_the_end_of_a_take():
    assert len(session.drop_enter_click(np.ones(SR, np.float32))) == SR - int(0.15 * SR)


def test_end_of_turn_saving_comes_from_the_verdicts_and_counts_premature_cuts_apart():
    complete = {"a": 0.9, "b": 0.9, "c": 0.2, "d": 0.8}
    at_pause = {"x": 0.3, "y": 0.7, "z": 0.9, "w": 0.2}
    pause_s = {"x": 1.0, "y": 1.2, "z": 0.4, "w": 2.5}
    out = bench_smart_turn.end_of_turn(complete, at_pause, pause_s, infer_ms=15)
    # Three turns end at 500 + 15 ms; "c" was judged incomplete and waits the 1.5 s extension too.
    assert out["complete_judged_incomplete"] == "1/4"
    assert out["end_ms_mean"] == 890
    assert out["saved_ms_vs_fixed_silence"] == {800: -90, 1000: 110, 1500: 610}
    # "y" is judged complete mid-pause; "w" outlasts the extension; "z" never reaches 500 ms of silence.
    assert out["premature_cuts"] == {"smart_turn": "2/4", "fixed_800ms": "3/4", "fixed_1000ms": "3/4", "fixed_1500ms": "1/4"}


def test_a_barge_in_counts_only_speech_the_same_jarvis_audio_did_not_already_trigger():
    frame_s = bench_aec.VAD_FRAME / SR
    probs = np.zeros(200)
    probs[0:16] = probs[100:116] = 0.9  # echo: the same Jarvis audio fires VAD in both halves
    probs[150:166] = 0.9  # the User, over a stretch where Jarvis's first half was quiet
    spans = [[100 * frame_s, 125 * frame_s], [150 * frame_s, 175 * frame_s]]
    out = bench_aec.barge_ins(probs, spans, shift_s=100 * frame_s)
    assert out == {"user_barge_ins_detected": "1/2", "matching_jarvis_span_triggered": "1/2"}


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
