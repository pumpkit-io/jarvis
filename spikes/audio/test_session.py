"""Checks for the recording-session kit that need no mic, no speakers and no models (`uv run test_session.py`)."""

import contextlib
import http.server
import io
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
import bench_claude_idle  # noqa: E402
import bench_smart_turn  # noqa: E402
import bench_stt  # noqa: E402
import bench_wake  # noqa: E402
import common  # noqa: E402
import report  # noqa: E402
import session  # noqa: E402

SR = 16_000
TRANSCRIPT_FIELDS = {"hyp", "samples", "wrong"}


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


def keys(data) -> set:
    if isinstance(data, dict):
        return set(data) | set().union(*map(keys, data.values()))
    if isinstance(data, list):
        return set().union(*map(keys, data))
    return set()


def test_tracked_results_from_recordings_hold_no_transcript_fields():
    root = common.HERE.parent.parent
    tracked = subprocess.run(["git", "ls-files", "spikes/audio/results/*.json"], cwd=root,
                             capture_output=True, text=True, check=True).stdout.split()
    # Only results on synthetic speech may carry transcripts; everything else may come from the User's recordings.
    for name in (n for n in tracked if not n.endswith("-synthetic.json")):
        assert not keys(json.loads((root / name).read_text())) & TRANSCRIPT_FIELDS, name


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
    # Jarvis's lines last 29 s per play, followed by a 4 s silent tail; each cue's window is 3 s.
    ref_s = 2 * 29.0 + 4.0
    cues = session.cue_times(ref_s)
    assert len(cues) == 5
    assert cues[0] > 29.0
    assert cues[-1] + 3.0 <= ref_s - 0.5
    assert all(b - a >= 3.0 for a, b in zip(cues, cues[1:]))


def test_cue_times_refuse_a_reference_too_short_for_five_cues():
    try:
        session.cue_times(2 * 10.0 + 4.0)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_vpio_marks_are_shifted_by_when_playback_began_in_the_recording():
    out = session.parse_helper_output("recording_started_at=1000.25\nplaying_at=1000.75\nwrote x.wav\n")
    m = session.marks([30.0, 36.0], out["playing_at"] - out["recording_started_at"], half_s=29.0)
    assert m["jarvis_only"] == [0.5, 30.5]
    assert m["echo_shift_s"] == 29.0
    assert m["user_speech"] == [[30.5, 33.5], [36.5, 39.5]]


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
    # An hour in 5-minute chunks is twelve chunks.
    assert session.room_chunks_todo(folder, redo=False) == list(range(1, 13))
    for i in (1, 2, 4):
        common.save_16k(folder / f"room_{i:02d}.wav", tone(300, 1, SR))
    assert session.room_chunks_todo(folder, redo=False) == [3] + list(range(5, 13))
    assert session.room_chunks_todo(folder, redo=True) == list(range(1, 13))


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
    assert len(session.drop_enter_click(np.ones(SR, np.float32))) == SR - 2400


def test_end_of_turn_saving_comes_from_the_verdicts_and_counts_premature_cuts_apart():
    complete = {"a": 0.9, "b": 0.9, "c": 0.2, "d": 0.8}
    # "z" pauses for less than the 500 ms VAD silence, so Smart Turn never ran on it and has no verdict.
    at_pause = {"x": 0.3, "y": 0.7, "w": 0.2}
    pause_s = {"x": 1.0, "y": 1.2, "z": 0.4, "w": 2.5}
    out = bench_smart_turn.end_of_turn(complete, at_pause, pause_s, infer_ms=15)
    # Three turns end at 500 + 15 ms; "c" was judged incomplete and waits the 1.5 s extension too.
    assert out["complete_judged_incomplete"] == "1/4"
    assert out["end_ms_mean"] == 890
    assert out["saved_ms_vs_fixed_silence"] == {800: -90, 1000: 110, 1500: 610}
    # "y" is judged complete mid-pause; "w" outlasts the extension; "z" never reaches 500 ms of silence.
    assert out["premature_cuts"] == {"smart_turn": "2/4", "fixed_800ms": "3/4", "fixed_1000ms": "3/4", "fixed_1500ms": "1/4"}


def test_a_barge_in_counts_only_speech_the_same_jarvis_audio_did_not_already_trigger():
    frame_s = 512 / SR  # Silero VAD's frame
    probs = np.zeros(200)
    probs[0:16] = probs[100:116] = 0.9  # echo: the same Jarvis audio fires VAD in both halves
    probs[150:166] = 0.9  # the User, over a stretch where Jarvis's first half was quiet
    spans = [[100 * frame_s, 125 * frame_s], [150 * frame_s, 175 * frame_s]]
    out = bench_aec.barge_ins(probs, spans, shift_s=100 * frame_s)
    assert out == {"user_barge_ins_detected": "1/2", "matching_jarvis_span_triggered": "1/2"}


def test_a_pause_of_exactly_the_vad_silence_is_cut_and_scored_alike():
    folder = SCRATCH / "pauses-boundary"
    common.save_16k(folder / "p_en01.wav", tone(200, 2.0, SR))
    saved = bench_smart_turn.find_pause
    try:
        # 0.57 - 0.07 is 0.49999999999999994 in floating point, yet the pause is 500 ms long.
        bench_smart_turn.find_pause = lambda audio: (0.07, 0.57)
        cut, _, pauses = bench_smart_turn.pause_turns(folder)
    finally:
        bench_smart_turn.find_pause = saved
    assert set(cut) == {"p_en01"} and pauses == {"p_en01": 0.5}
    out = bench_smart_turn.end_of_turn({"a": 0.9}, {"p_en01": 0.8}, pauses, infer_ms=15)
    assert out["premature_cuts"]["smart_turn"] == "1/1"


def test_a_pause_long_enough_but_without_a_verdict_is_not_read_as_one():
    out = bench_smart_turn.end_of_turn({"a": 0.9}, {}, {"x": 0.6}, infer_ms=15)
    assert out["premature_cuts"]["smart_turn"] == "0/1"


def test_smart_turn_hears_the_turn_up_to_half_a_second_into_the_silence():
    rng = np.random.default_rng(0)
    room_tone = rng.normal(0, 0.002, int(0.8 * SR)).astype(np.float32)
    long_tail = np.concatenate([tone(200, 1.0, SR), room_tone])
    heard = bench_smart_turn.turn_audio(long_tail, 1.0)
    assert len(heard) == int(1.5 * SR)
    assert np.array_equal(heard, long_tail[: int(1.5 * SR)])
    short_tail = np.concatenate([tone(200, 1.0, SR), room_tone[: int(0.1 * SR)]])
    heard = bench_smart_turn.turn_audio(short_tail, 1.0)
    assert len(heard) == int(1.5 * SR)
    assert np.array_equal(heard[: int(1.1 * SR)], short_tail) and not heard[int(1.1 * SR) :].any()


def test_a_complete_turn_ends_where_its_speech_ends():
    take = np.concatenate([tone(200, 1.0, SR), np.zeros(int(0.3 * SR), np.float32)])
    assert abs(bench_smart_turn.speech_end_s(take) - 1.0) < 0.011


def test_minutes_where_the_video_had_stopped_are_counted_as_quiet():
    rng = np.random.default_rng(0)
    video = (rng.normal(0, 0.05, 60 * SR) * 32767).astype(np.int16)
    stopped = (rng.normal(0, 0.0005, 60 * SR) * 32767).astype(np.int16)
    assert bench_wake.quiet_seconds(np.concatenate([video, stopped, video, stopped[: 30 * SR]])) == 90.0


def test_false_accepts_per_hour_leave_quiet_minutes_out_of_the_hours():
    # Two hours recorded, half an hour of it with the video stopped: 3 false accepts over 1.5 h.
    assert bench_wake.per_hour({0.5: 3}, seconds=7200, quiet_s=1800) == {"@0.5": 2.0}


def test_a_missing_swift_compiler_means_no_helper_instead_of_a_crash():
    saved = os.environ["PATH"]
    try:
        os.environ["PATH"] = str(SCRATCH / "empty-bin")
        assert session.compile_helper() is None
    finally:
        os.environ["PATH"] = saved


def helper_stub(body: str) -> Path:
    stub = SCRATCH / "vpio-helper-stub"
    stub.write_text(f"#!/bin/sh\necho $$ > {SCRATCH / 'helper.pid'}\n{body}\n")
    stub.chmod(0o755)
    return stub


def test_a_helper_that_never_starts_playing_fails_the_pass_and_is_stopped():
    run = SCRATCH / "vpio-silent"
    run.mkdir()
    saved = session.PLAYING_AT_TIMEOUT_S
    try:
        session.PLAYING_AT_TIMEOUT_S = 1
        t = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()):
            out = session.vpio_pass(helper_stub("echo recording_started_at=1.0\nexec sleep 60"), run, 62.0, [33.0])
        assert time.monotonic() - t < 15
    finally:
        session.PLAYING_AT_TIMEOUT_S = saved
    assert "failed" in out and "playing" in out["failed"]
    pid = int((SCRATCH / "helper.pid").read_text())
    assert subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode != 0, "helper still running"


def test_closing_the_terminal_still_stops_what_the_session_started():
    child = SCRATCH / "child.pid"
    script = (f"import sys, subprocess, time; sys.path.insert(0, {str(common.HERE)!r}); import session\n"
              "session.install_signal_handlers()\n"
              "proc = subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
              f"open({str(child)!r}, 'w').write(str(proc.pid))\n"
              "try:\n    time.sleep(60)\nfinally:\n    session.stop(proc)\n")
    for sig in ("SIGHUP", "SIGTERM"):
        child.unlink(missing_ok=True)
        proc = subprocess.Popen([sys.executable, "-c", script])
        deadline = time.monotonic() + 30
        while not child.exists() or not child.read_text():
            assert time.monotonic() < deadline, "the session never started its child"
            time.sleep(0.1)
        proc.send_signal(getattr(__import__("signal"), sig))
        proc.wait(15)
        pid = int(child.read_text())
        assert subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode != 0, f"child survived {sig}"


def test_an_orphan_on_the_turbo_port_is_named_and_not_measured():
    server, port = serve(200)
    saved = session.TURBO_PORT, session.TURBO_MODEL, session.WHISPER_SERVER
    model = SCRATCH / "model-orphan.bin"
    model.write_bytes(b"")
    try:
        session.TURBO_PORT, session.TURBO_MODEL, session.WHISPER_SERVER = port, model, stub_server("exit 0")
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            assert session.start_turbo_server() is None
    finally:
        session.TURBO_PORT, session.TURBO_MODEL, session.WHISPER_SERVER = saved
        server.shutdown()
    assert f"pid {os.getpid()}" in said.getvalue()


def test_the_turbo_port_is_measured_only_when_it_is_the_server_the_session_started():
    assert "nothing" in bench_stt.server_skip("server-2023", listener_pid=None, turbo_pid=None)
    assert "not started by the session" in bench_stt.server_skip("server-2023", listener_pid=999, turbo_pid=None)
    assert "999" in bench_stt.server_skip("server-2023", listener_pid=999, turbo_pid=123)
    assert bench_stt.server_skip("server-2023", listener_pid=123, turbo_pid=123) is None
    assert bench_stt.server_skip("server-2022", listener_pid=555, turbo_pid=None) is None
    stt_cmd = session.benches(SCRATCH / "benches", turbo_pid=123)[0]
    assert stt_cmd[0] == "bench_stt.py" and stt_cmd[-2:] == ["--turbo-pid", "123"]
    assert "--turbo-pid" not in session.benches(SCRATCH / "benches")[0]


def run_stt_session(turbo_pid: int, port: int) -> tuple[int, str]:
    """Run bench_stt's parent with one stub engine; the parent starts this stub as its child process, which sees a real listener on `port`."""
    stub = SCRATCH / "stt-stub"
    stub.mkdir(exist_ok=True)
    here = Path(__file__).resolve().parent
    (stub / "bench_stt.py").write_text(
        f"import sys\nsys.path.insert(0, {str(here)!r})\nimport bench_stt\n"
        f"import pathlib\nbench_stt.HERE = pathlib.Path(__file__).resolve().parent\n"
        f"bench_stt.ENGINES = ['server-2023']\nbench_stt.SERVER_PORTS = {{'server-2023': {port}}}\n"
        "bench_stt.run_engine = lambda engine, source: {'engine': engine, 'source': source, 'clips': {}}\n"
        "bench_stt.main()\n")
    (SCRATCH / "results" / "stt-server-2023-synthetic.json").unlink(missing_ok=True)
    done = subprocess.run([sys.executable, str(stub / "bench_stt.py"), "--turbo-pid", str(turbo_pid)],
                          capture_output=True, text=True, env={**os.environ, "JARVIS_AUDIO_RESULTS": str(SCRATCH / "results")})
    return done.returncode, done.stdout + done.stderr


def test_a_full_stt_run_hands_the_turbo_pid_to_the_child_that_measures_it():
    server, port = serve(200)
    try:
        code, said = run_stt_session(turbo_pid=os.getpid(), port=port)
    finally:
        server.shutdown()
    assert code == 0, said
    assert (SCRATCH / "results" / "stt-server-2023-synthetic.json").exists(), said


def test_a_turbo_row_the_session_expected_but_skipped_fails_the_run():
    server, port = serve(200)
    try:
        code, said = run_stt_session(turbo_pid=os.getpid() + 1, port=port)
    finally:
        server.shutdown()
    assert code != 0 and "server-2023" in said
    assert not (SCRATCH / "results" / "stt-server-2023-synthetic.json").exists()


def test_a_bench_that_hangs_is_stopped_with_everything_it_started():
    child = SCRATCH / "bench-child.pid"
    script = f"import subprocess, time; p = subprocess.Popen(['sleep', '60']); open({str(child)!r}, 'w').write(str(p.pid)); time.sleep(60)"
    t = time.monotonic()
    with open(SCRATCH / "bench.log", "w") as log:
        assert session.run_bench([sys.executable, "-c", script], log, timeout_s=2) is None
    assert time.monotonic() - t < 15
    time.sleep(0.2)
    pid = int(child.read_text())
    assert subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode != 0, "the bench's child survived"
    with open(SCRATCH / "bench.log", "w") as log:
        assert session.run_bench([sys.executable, "-c", "raise SystemExit(4)"], log, timeout_s=10) == 4


def test_a_failed_claude_spawn_still_ends_the_sessions_already_started_with_their_children():
    bin_dir, child = SCRATCH / "claude-bin", SCRATCH / "claude-child.pid"
    bin_dir.mkdir()
    # Stands in for `claude` and an MCP server it starts, one that ignores the hangup when the session's pty closes.
    (bin_dir / "claude").write_text(f"#!/bin/sh\ntrap '' HUP\nsleep 60 &\necho $! > {child}\nexec sleep 60\n")
    (bin_dir / "claude").chmod(0o755)
    real_spawn, spawned = bench_claude_idle.spawn, []

    def spawn_once(cwd):
        if spawned:
            raise OSError("out of ptys")
        spawned.append(real_spawn(cwd))
        deadline = time.monotonic() + 10
        while not child.exists() or not child.read_text().strip():
            assert time.monotonic() < deadline, "the stub session never started its child"
            time.sleep(0.05)
        return spawned[0]

    saved = os.environ["PATH"]
    try:
        os.environ["PATH"] = f"{bin_dir}:{saved}"
        bench_claude_idle.spawn = spawn_once
        bench_claude_idle.idle_footprints(SCRATCH, settle_s=0)
    except OSError:
        pass
    else:
        raise AssertionError("expected the failed spawn to propagate")
    finally:
        bench_claude_idle.spawn, os.environ["PATH"] = real_spawn, saved
    time.sleep(0.2)
    for pid in (spawned[0][0], int(child.read_text())):
        assert subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode != 0, f"{pid} still running"


def test_the_headphones_run_is_analysed_only_when_it_was_recorded():
    recordings = SCRATCH / "benches"
    assert not any("headphones-run" in " ".join(cmd) for cmd in session.benches(recordings))
    common.save_16k(recordings / "headphones-run" / "mic.wav", tone(300, 1, SR))
    assert any(cmd[:2] == ["bench_aec.py", "analyse"] and cmd[2].endswith("headphones-run") for cmd in session.benches(recordings))


class FakePortAudio:
    """sounddevice as PortAudio sees it: the device list is read once per initialisation."""

    def __init__(self, *device_lists):
        self.lists, self.now = list(device_lists), device_lists[0]

    def _terminate(self):
        pass

    def _initialize(self):
        self.now = self.lists.pop(1) if len(self.lists) > 1 else self.now

    def query_devices(self, kind=None):
        if kind == "output":
            return self.now[self.now["default_output"]]
        return [d for k, d in self.now.items() if k != "default_output"]


def device(index: int, name: str, inputs: int, outputs: int) -> dict:
    return {"index": index, "name": name, "max_input_channels": inputs, "max_output_channels": outputs}


def test_headphones_connected_after_start_are_found_and_played_to():
    at_start = {0: device(0, "MacBook Pro Microphone", 1, 0), 1: device(1, "MacBook Pro Speakers", 0, 2), "default_output": 1}
    plugged = {0: device(0, "AirPods Pro", 1, 2), 1: device(1, "MacBook Pro Microphone", 1, 0),
               2: device(2, "MacBook Pro Speakers", 0, 2), "default_output": 0}
    saved = session.sd
    try:
        session.sd = FakePortAudio(at_start, at_start, plugged)
        assert session.headphones_devices() is None
        assert session.headphones_devices() == (1, 0)
    finally:
        session.sd = saved


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
