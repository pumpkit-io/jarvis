"""Ticket #5's recording session with the User, then every measurement on what it recorded (`uv run session.py`).

Steps and usage are in docs/notes/phase-0-spikes.md, ## Audio > The recording session.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import sounddevice as sd
import soundfile as sf

from common import DATA, HERE, RECORDINGS, RESULTS, SR, find_pause, resample, save_16k
from corpus import CLIPS, JARVIS_LINES, PAUSE, PAUSE_TAKES, TAKES_PER_PHRASING, WAKE_PHRASINGS

PLAY_SR = 48_000
WAKE_TAKE_S = 2.5
VIDEO_MINUTES = 5
ROOM_MINUTES = 60
ROOM_CHUNK_MINUTES = 5
ENTER_CLICK_S = 0.15
TURBO_MODEL = Path.home() / ".cache/jarvis-spikes/whisper/ggml-large-v3-turbo.bin"
WHISPER_SERVER = Path.home() / ".voicemode/services/whisper/build/bin/whisper-server"
TURBO_PORT = 2023
SERVER_READY_TIMEOUT_S = 120
PLAYING_AT_TIMEOUT_S = 30
# A wedged STT server can hold a request open for good; no bench needs this long on the session's audio.
BENCH_TIMEOUT_S = 40 * 60
STEPS = ["preflight", "baseline", "clips", "pauses", "wake", "speakers", "headphones", "room", "analyse"]

# What the User says over Jarvis in the second half of the speaker run.
BARGE_IN_WORDS = ["okay", "mhm", "wait, stop", "aspetta", "actually, open an issue instead"]
USER_SPAN_S = 3.0  # the window after a cue in which the User's words count as detected
SPEAKER_TAIL_S = 4.0  # silence after Jarvis so the last cue's words are recorded


def say(text: str) -> None:
    print(f"\n{text}")


def wait_enter(prompt: str = "Press Enter to go on.") -> str:
    return input(f"  {prompt} ").strip().lower()


def loud(text: str) -> None:
    bar = "!" * 72
    print(f"\n{bar}\n{text}\n{bar}\n", flush=True)


def record_until_enter() -> np.ndarray:
    chunks: list[np.ndarray] = []
    with sd.InputStream(samplerate=SR, channels=1, dtype="float32", callback=lambda d, *_: chunks.append(d.copy())):
        input("  ● recording... press Enter when you finish speaking. ")
    audio = np.concatenate(chunks)[:, 0] if chunks else np.zeros(0, np.float32)
    return drop_enter_click(audio)


def drop_enter_click(audio: np.ndarray) -> np.ndarray:
    return audio[: max(0, len(audio) - int(ENTER_CLICK_S * SR))]


def record_after_cue(cue: str, seconds: float) -> np.ndarray:
    """Open the mic, show the cue once audio is flowing, and return `seconds` of audio from the cue on."""
    chunks: list[np.ndarray] = []
    flowing = threading.Event()

    def callback(data, *_):
        chunks.append(data.copy())
        flowing.set()

    with sd.InputStream(samplerate=SR, channels=1, dtype="float32", callback=callback):
        if not flowing.wait(3):
            sys.exit("  the microphone delivered no audio: check the input device and microphone access.")
        skip = sum(len(c) for c in list(chunks))
        print(cue, flush=True)
        time.sleep(seconds + 0.1)
    audio = np.concatenate(chunks)[:, 0]
    return audio[skip : skip + int(seconds * SR)]


def level_db(audio: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(audio**2)) + 1e-9))


def preflight() -> None:
    say("Preflight. Use the MacBook's built-in microphone and speakers; unplug headphones.")
    print(f"  input:  {sd.query_devices(kind='input')['name']}")
    print(f"  output: {sd.query_devices(kind='output')['name']}")
    wait_enter("Enter, then say a few words when the dot appears (3 s).")
    audio = record_after_cue("  ● speak now", 3)
    print(f"  level {level_db(audio):.0f} dBFS (speech should be above -40; silence near -60 or below)")
    if np.abs(audio).max() < 1e-4:
        sys.exit("  the mic recorded silence: grant this terminal microphone access in System Settings > Privacy.")
    holder = port_holder(TURBO_PORT)
    if holder:
        loud(f"{holder} already listens on :{TURBO_PORT}. Stop it before the analysis, or the turbo row is left out.")


def baseline() -> None:
    say("Baseline (Ticket #4). This step points at Ticket #4's hook-based harness in spikes/agent/baseline/ of this\n"
        "checkout. Its steps are in docs/notes/phase-0-spikes.md, ## Agent SDK > Baseline. In short, in another\n"
        "terminal from the repo root:\n"
        "  ./jarvis --settings spikes/agent/baseline/settings.json   # about 10 turns, EN and IT, no repo work\n"
        "  python3 spikes/agent/baseline/analyze.py                  # prints p50 and p90")
    wait_enter("Press Enter when it is done, or type s and Enter to skip.")


def clips(redo: bool) -> None:
    say(f"Clips: {len(CLIPS)} sentences. Read each at your normal pace and distance, as you would talk to Jarvis.")
    for n, clip in enumerate(CLIPS, 1):
        path = RECORDINGS / "clips" / f"{clip.id}.wav"
        if path.exists() and not redo:
            continue
        while True:
            print(f"\n  [{n}/{len(CLIPS)}] ({clip.lang}) {clip.text}")
            wait_enter("Enter to start recording.")
            audio = record_until_enter()
            print(f"  {len(audio) / SR:.1f} s, {level_db(audio):.0f} dBFS")
            if wait_enter("Enter to keep, r to redo:") != "r":
                save_16k(path, audio)
                break


def pauses(redo: bool) -> None:
    say(f"Pauses: {len(PAUSE_TAKES)} sentences for end-of-turn detection. Where you see {PAUSE.strip()}, stop for about\n"
        "a second, as if looking for the next word, then finish the sentence. Sentences without it, say straight through.")
    for n, take in enumerate(PAUSE_TAKES, 1):
        path = RECORDINGS / "pauses" / f"{take.id}.wav"
        if path.exists() and not redo:
            continue
        while True:
            print(f"\n  [{n}/{len(PAUSE_TAKES)}] ({take.lang}) {take.text}")
            wait_enter("Enter to start recording.")
            audio = record_until_enter()
            pause = find_pause(audio)
            heard = f"pause of {pause[1] - pause[0]:.1f} s at {pause[0]:.1f} s" if pause else "no pause heard"
            print(f"  {len(audio) / SR:.1f} s, {level_db(audio):.0f} dBFS, {heard}")
            if take.paused and not pause:
                print(f"  I need the pause in this one: stop for about a second at {PAUSE.strip()}.")
                if wait_enter("Enter to record it again, k to keep it anyway:") != "k":
                    continue
                save_16k(path, audio)
                break
            if wait_enter("Enter to keep, r to redo:") != "r":
                save_16k(path, audio)
                break


def wake(redo: bool) -> None:
    say(f"Wake word: each phrasing {TAKES_PER_PHRASING} times. Say it when the dot appears; vary tone and distance a little.")
    for key, phrase in WAKE_PHRASINGS.items():
        todo = [t for t in range(1, TAKES_PER_PHRASING + 1)
                if redo or not (RECORDINGS / "wake" / f"{key}_{t:02d}.wav").exists()]
        if not todo:
            continue
        wait_enter(f'Next: "{phrase}". Enter to start.')
        for take in todo:
            audio = record_after_cue(f'  ● "{phrase}"  ({take}/{TAKES_PER_PHRASING})', WAKE_TAKE_S)
            save_16k(RECORDINGS / "wake" / f"{key}_{take:02d}.wav", audio)
            time.sleep(0.4)


def jarvis_reference() -> np.ndarray:
    """Jarvis's lines twice over at 48 kHz, then a silent tail: one half alone, one half under the User's words."""
    path = RECORDINGS / "speaker-run" / "reference_48k.wav"
    if path.exists():
        return sf.read(path, dtype="float32")[0]
    print("  synthesising Jarvis's lines with kokoro-mlx...")
    from kokoro_mlx import KokoroTTS
    from common import prepare_tts

    prepare_tts()

    tts = KokoroTTS.from_pretrained("mlx-community/Kokoro-82M-bf16")
    parts = []
    for lang, line in JARVIS_LINES:
        audio = np.asarray(tts.generate(line, voice="am_liam" if lang == "en" else "im_nicola").audio, np.float32)
        parts += [resample(audio, tts.SAMPLE_RATE, PLAY_SR), np.zeros(PLAY_SR // 2, np.float32)]
    half = np.concatenate(parts)
    ref = np.concatenate([half, half, np.zeros(int(SPEAKER_TAIL_S * PLAY_SR), np.float32)]) * 0.8
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, ref, PLAY_SR)
    return ref


def jarvis_half_s(ref_s: float) -> float:
    """Length of one play of Jarvis's lines in a reference of `ref_s` seconds."""
    return (ref_s - SPEAKER_TAIL_S) / 2


def cue_times(ref_s: float) -> list[float]:
    """Seconds after playback starts at which each Barge-in cue shows, spread over the second half of Jarvis."""
    half = jarvis_half_s(ref_s)
    step = (half - 2.0) / len(BARGE_IN_WORDS)
    if step < USER_SPAN_S:
        raise ValueError(f"Jarvis's half of {half:.1f} s is too short for {len(BARGE_IN_WORDS)} cues")
    return [round(half + 1.0 + i * step, 2) for i in range(len(BARGE_IN_WORDS))]


def show_cues(elapsed, times: list[float]) -> None:
    """Print each cue when `elapsed()` reaches its time."""
    for at, words in zip(times, BARGE_IN_WORDS):
        delay = at - elapsed()
        if delay > 0:
            time.sleep(delay)
        print(f'  ● say: "{words}"', flush=True)


def marks(cues: list[float], offset: float, half_s: float) -> dict:
    """Where the User spoke and where Jarvis was alone, in seconds of a recording whose playback began at `offset`.

    `echo_shift_s` maps a User window back onto the same Jarvis audio in the first play, for bench_aec's echo check.
    """
    return {"jarvis_only": [round(max(0.0, offset), 2), round(offset + cues[0], 2)],
            "user_speech": [[round(offset + c, 2), round(offset + c + USER_SPAN_S, 2)] for c in cues],
            "echo_shift_s": round(half_s, 4)}


def parse_helper_output(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in ("recording_started_at", "playing_at"):
            out[key] = float(value)
    return out


def vpio_failure(returncode: int | None, times: dict, wav: Path) -> str | None:
    """Why a Voice Processing I/O run can't be scored, or None when it can."""
    if returncode:
        return f"helper exited with {returncode}"
    if not {"recording_started_at", "playing_at"} <= times.keys():
        return "helper never reported when recording and playback started"
    if not wav.exists():
        return "no output file"
    audio, _ = sf.read(wav, dtype="float32")
    if len(audio) < SR:
        return "output shorter than a second"
    if np.abs(audio).max() < 1e-4:
        return "output is silent (microphone access?)"
    return None


def vpio_pass(helper: Path, run: Path, ref_s: float, cues: list[float]) -> dict:
    """One Voice Processing I/O run; returns its marks in vpio.wav's seconds, or {"failed": reason}."""
    wav = run / "vpio.wav"
    wav.unlink(missing_ok=True)
    proc = subprocess.Popen([str(helper), str(run / "reference_48k.wav"), str(wav)], stdout=subprocess.PIPE, text=True)
    lines: list[str] = []
    playing = threading.Event()

    def read() -> None:
        for line in proc.stdout:
            lines.append(line)
            if line.startswith("playing_at="):
                playing.set()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reason = None
    try:
        deadline = time.monotonic() + PLAYING_AT_TIMEOUT_S
        while not playing.wait(0.2) and proc.poll() is None and time.monotonic() < deadline:
            pass
        if playing.is_set():
            started = parse_helper_output("".join(lines))["playing_at"]
            show_cues(lambda: time.time() - started, cues)
            proc.wait(timeout=ref_s + 30)
        elif proc.poll() is None:
            reason = f"helper did not start playing within {PLAYING_AT_TIMEOUT_S} s"
    except subprocess.TimeoutExpired:
        reason = "helper timed out"
    except BaseException:
        stop(proc)
        wav.unlink(missing_ok=True)
        raise
    finally:
        stop(proc)
        reader.join(5)
    times = parse_helper_output("".join(lines))
    reason = reason or vpio_failure(proc.returncode, times, wav)
    if reason:
        wav.unlink(missing_ok=True)
        return {"failed": reason}
    return marks(cues, times["playing_at"] - times["recording_started_at"], jarvis_half_s(ref_s))


def vpio_done(run: Path, segments: dict) -> bool:
    """A VPIO pass counts only with both its recording and the marks placing the cues in it."""
    return (run / "vpio.wav").exists() and "user_speech" in segments.get("vpio", {})


def compile_helper() -> Path | None:
    helper = DATA / "vpio_playrec"
    source = HERE / "vpio_playrec.swift"
    if helper.exists() and helper.stat().st_mtime >= source.stat().st_mtime:
        return helper
    print("  compiling the Voice Processing I/O helper...")
    helper.parent.mkdir(parents=True, exist_ok=True)
    try:
        if subprocess.run(["swiftc", "-O", str(source), "-o", str(helper)], capture_output=True).returncode:
            return None
    except FileNotFoundError:
        return None
    return helper


def play_and_record(ref: np.ndarray, cues: list[float], mic: int | None = None, speaker: int | None = None) -> np.ndarray:
    """Play `ref` and record the mic on one duplex stream, showing each cue on the clock of the speaker output."""
    rec = np.zeros(len(ref), np.float32)
    pos, clock = 0, {}
    started, done = threading.Event(), threading.Event()

    def callback(indata, outdata, frames, time_info, status):
        nonlocal pos
        if not clock:
            latency = time_info.outputBufferDacTime - time_info.currentTime
            # The cue clock starts when the first sample reaches the speaker, not when the stream was asked to open.
            clock["t0"] = time.monotonic() + (latency if 0 <= latency < 1 else 0.0)
            started.set()
        n = min(frames, len(ref) - pos)
        outdata[:n, 0] = ref[pos : pos + n]
        outdata[n:] = 0
        # Input and output share the callback's sample index, so the recording stays aligned with the reference.
        rec[pos : pos + n] = indata[:n, 0]
        pos += n
        if pos >= len(ref):
            raise sd.CallbackStop

    with sd.Stream(samplerate=PLAY_SR, channels=1, dtype="float32", device=(mic, speaker), callback=callback,
                   finished_callback=done.set):
        if not started.wait(5):
            sys.exit("  the audio stream never started: check the built-in speakers and microphone access.")
        show_cues(lambda: time.monotonic() - clock["t0"], cues)
        done.wait(len(ref) / PLAY_SR + 10)
    return rec


def speakers(redo: bool) -> None:
    run = RECORDINGS / "speaker-run"
    say("Speaker run. Built-in speakers at your usual volume, no headphones. Jarvis talks for about a minute.\n"
        "Stay silent for the first half. In the second half, say the words shown, over Jarvis.")
    ref = jarvis_reference()
    ref_s = len(ref) / PLAY_SR
    cues = cue_times(ref_s)
    segments_path = run / "segments.json"
    segments = json.loads(segments_path.read_text()) if segments_path.exists() else {}
    if redo or not (run / "mic.wav").exists():
        wait_enter("Enter to start (WebRTC pass: raw mic).")
        segments = save_run(run, ref, play_and_record(ref, cues), cues) | {"vpio": segments.get("vpio", {})}
        segments_path.write_text(json.dumps(segments))
    if redo or not vpio_done(run, segments):
        helper = compile_helper()
        while True:
            if helper is None:
                segments["vpio"] = {"failed": "the Swift helper did not compile"}
                loud("The Voice Processing I/O helper did not compile (swiftc). The WebRTC numbers still stand.")
                break
            wait_enter("Same again, through macOS Voice Processing I/O. Enter to start.")
            segments["vpio"] = vpio_pass(helper, run, ref_s, cues)
            segments_path.write_text(json.dumps(segments))
            if "failed" not in segments["vpio"]:
                break
            loud(f"The Voice Processing I/O run FAILED: {segments['vpio']['failed']}.")
            if wait_enter("Enter to try again, s to skip it (the WebRTC numbers still stand):") == "s":
                break
        segments_path.write_text(json.dumps(segments))
    if redo or not (run / "video.wav").exists():
        say(f"Now start a video or podcast with people talking on this Mac, at normal volume. I record {VIDEO_MINUTES} minutes.\n"
            "Stay quiet and don't say Jarvis. This measures false wakes with a video playing.")
        wait_enter("Enter once the video is playing.")
        save_16k(run / "video.wav", record_after_cue("  ● recording the room", VIDEO_MINUTES * 60))
        print("  done.")


def save_run(run: Path, ref: np.ndarray, rec: np.ndarray, cues: list[float]) -> dict:
    """Save a duplex run's mic and reference at 16 kHz; returns its marks for segments.json."""
    save_16k(run / "mic.wav", resample(rec, PLAY_SR, SR))
    save_16k(run / "reference.wav", resample(ref, PLAY_SR, SR))
    return {**marks(cues, 0.0, jarvis_half_s(len(ref) / PLAY_SR)), "cues": dict(zip(BARGE_IN_WORDS, cues))}


def builtin_mic() -> int | None:
    """The MacBook's own microphone, so a headset's mic doesn't take over the input."""
    for i, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] and "MacBook" in device["name"]:
            return i
    return None


def refresh_devices() -> None:
    """Make PortAudio see the devices connected or removed since it started: it reads the list once per initialisation."""
    sd._terminate()
    sd._initialize()


def headphones_devices() -> tuple[int | None, int] | None:
    """The built-in mic and the current default output, or None while the default output is still the built-in speakers."""
    refresh_devices()
    output = sd.query_devices(kind="output")
    print(f"  output: {output['name']}")
    if "MacBook" in output["name"]:
        return None
    return builtin_mic(), output["index"]


def headphones(redo: bool) -> None:
    run = RECORDINGS / "headphones-run"
    if (run / "mic.wav").exists() and not redo:
        return
    say("Headphones, optional, under 3 minutes. Jarvis plays the same lines through headphones, with no echo cancellation,\n"
        "and the built-in mic records. As before: silent for the first half, then say the words shown, over Jarvis.")
    ref = jarvis_reference()
    cues = cue_times(len(ref) / PLAY_SR)
    while True:
        if wait_enter("Put them on, then Enter. No headphones to hand: type s and Enter.") == "s":
            return
        devices = headphones_devices()
        if devices:
            break
        loud("Sound still goes to the built-in speakers. Connect the headphones, or pick them in the menu bar's sound menu.")
    mic, phones = devices
    try:
        rec = play_and_record(ref, cues, mic=mic, speaker=phones)
        (run / "segments.json").write_text(json.dumps(save_run(run, ref, rec, cues)))
    except sd.PortAudioError as err:
        loud(f"The headphones run FAILED ({err}); the speaker numbers still stand.")
    say("Take the headphones off and disconnect them: the room step plays its video on the built-in speakers.")
    wait_enter()
    refresh_devices()


def room_chunks_todo(folder: Path, redo: bool) -> list[int]:
    """Numbers of the ROOM_CHUNK_MINUTES chunks still to record; a chunk is saved only once complete."""
    n = ROOM_MINUTES // ROOM_CHUNK_MINUTES
    return [i for i in range(1, n + 1) if redo or not (folder / f"room_{i:02d}.wav").exists()]


def room(redo: bool) -> None:
    folder = RECORDINGS / "room"
    todo = room_chunks_todo(folder, redo)
    if not todo:
        return
    minutes = len(todo) * ROOM_CHUNK_MINUTES
    say(f"Room run, unattended: {minutes} minutes with a video playing, for false wakes per hour. Then the analysis runs on its own.\n"
        "Start a long video or podcast with people talking (an hour or more, or a playlist), at normal volume on the\n"
        "built-in speakers. Keep the lid open and the Mac on power. Then leave: nobody should speak near it.")
    wait_enter("Enter once the video is playing, then walk away.")
    try:
        for i in todo:
            audio = record_after_cue(f"  ● room {i}/{ROOM_MINUTES // ROOM_CHUNK_MINUTES} ({ROOM_CHUNK_MINUTES} min)",
                                     ROOM_CHUNK_MINUTES * 60)
            save_16k(folder / f"room_{i:02d}.wav", audio)
            print(f"    saved, {level_db(audio):.0f} dBFS", flush=True)
    except KeyboardInterrupt:
        left = len(room_chunks_todo(folder, False)) * ROOM_CHUNK_MINUTES
        loud(f"Room run stopped with {left} minutes to go; finished chunks are kept.\n"
             "To resume: uv run session.py --only room   then: uv run session.py --only analyse")
        sys.exit(1)
    print("  Room run done; the video can stop whenever you are back.")


def server_ready(port: int) -> bool:
    """True once a server on `port` answers with a success status, not merely holds the port."""
    try:
        return httpx.get(f"http://127.0.0.1:{port}/", timeout=1).is_success
    except httpx.TransportError:
        return False


def port_holder(port: int) -> str | None:
    """What listens on `port` here, as "name (pid N)", or None when nothing does."""
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"], capture_output=True, text=True).stdout
    except OSError:
        out = ""
    fields = {line[0]: line[1:] for line in out.splitlines() if line[:1] in ("p", "c")}
    if "p" in fields:
        return f"{fields.get('c', 'a process')} (pid {fields['p']})"
    with socket.socket() as sock:
        return "an unknown process" if sock.connect_ex(("127.0.0.1", port)) == 0 else None


def stop(proc: subprocess.Popen | None) -> None:
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def start_turbo_server() -> subprocess.Popen | None:
    """Start whisper.cpp large-v3-turbo on :2023 and wait until it answers; None, said loudly, when it can't."""
    missing = [str(p) for p in (TURBO_MODEL, WHISPER_SERVER) if not p.exists()]
    if missing:
        loud(f"No whisper.cpp large-v3-turbo row: missing {', '.join(missing)}.")
        return None
    holder = port_holder(TURBO_PORT)
    if holder:
        loud(f"{holder} already listens on :{TURBO_PORT}, perhaps a turbo server left by an earlier run. It is not\n"
             f"measured, so there is no turbo row. Stop it, then rerun: uv run session.py --only analyse")
        return None
    log = DATA / "whisper-server-2023.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([str(WHISPER_SERVER), "--host", "127.0.0.1", "--port", str(TURBO_PORT), "--model", str(TURBO_MODEL),
                             "--inference-path", "/v1/audio/transcriptions", "--threads", "11", "--convert"],
                            stdout=log.open("w"), stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + SERVER_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                loud(f"The whisper.cpp turbo server exited with {proc.returncode}; no turbo row. Log: {log}")
                return None
            if server_ready(TURBO_PORT):
                return proc
            time.sleep(0.5)
        loud(f"The whisper.cpp turbo server did not answer within {SERVER_READY_TIMEOUT_S} s; no turbo row. Log: {log}")
        stop(proc)
        return None
    except BaseException:
        stop(proc)
        raise


def benches(recordings: Path, turbo_pid: int | None = None) -> list[list[str]]:
    """The analysis commands, with the headphones run only when the User recorded it and :2023 only as the session's server."""
    run, phones = recordings / "speaker-run", recordings / "headphones-run"
    cmds = [
        ["bench_stt.py", "--source", "recordings"] + (["--turbo-pid", str(turbo_pid)] if turbo_pid else []),
        ["bench_lang.py", "--source", "recordings"],
        ["bench_wake.py", "recall", "--source", "recordings"],
        ["bench_wake.py", "false-accepts", f"{run}/video.wav", "--name", "false-accepts-video"],
        ["bench_wake.py", "false-accepts", f"{recordings}/room", "--name", "false-accepts-room"],
        ["bench_aec.py", "analyse", str(run)],
        ["bench_smart_turn.py", "--source", "recordings"],
    ]
    if (phones / "mic.wav").exists():
        cmds.insert(6, ["bench_aec.py", "analyse", str(phones)])
    return cmds


def run_bench(argv: list[str], out, timeout_s: float) -> int | None:
    """Run one bench; its exit code, or None when it ran past `timeout_s` and was stopped with every process it started."""
    proc = subprocess.Popen(argv, cwd=HERE, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        return proc.wait(timeout_s)
    except subprocess.TimeoutExpired:
        return None
    finally:
        # The bench runs in its own process group, so this also ends any engine process it left behind.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def analyse() -> None:
    say("Analysis. This runs on its own for about 18 minutes; you can leave.")
    log = DATA / "analysis.log"
    failed = []
    turbo = None
    try:
        turbo = start_turbo_server()
        with log.open("w") as out:
            for cmd in benches(RECORDINGS, turbo.pid if turbo else None):
                print(f"  $ {' '.join(cmd)}", flush=True)
                code = run_bench([sys.executable, str(HERE / cmd[0])] + cmd[1:], out, BENCH_TIMEOUT_S)
                if code is None:
                    failed.append(f"{' '.join(cmd)} (stopped after {BENCH_TIMEOUT_S // 60} min)")
                elif code:
                    failed.append(f"{' '.join(cmd)} (exit {code})")
    finally:
        stop(turbo)
    if failed:
        loud("These steps FAILED; their rows are missing. Details in " + str(log) + ":\n  " + "\n  ".join(failed))
    subprocess.run([sys.executable, str(HERE / "report.py")], cwd=HERE, check=False)
    print(f"  numbers: {RESULTS}/*-recordings.json, aec-speaker-run.json, aec-headphones-run.json, "
          "wake-false-accepts-video.json, wake-false-accepts-room.json")


def install_signal_handlers() -> None:
    """Turn a closed terminal or a kill into an exit that runs every cleanup, so no server or helper outlives the session."""
    def leave(signum, _frame):
        raise SystemExit(128 + signum)

    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, leave)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=STEPS)
    ap.add_argument("--redo", action="store_true")
    args = ap.parse_args()
    steps = [args.only] if args.only else STEPS
    actions = {"preflight": preflight, "baseline": baseline, "clips": lambda: clips(args.redo),
               "pauses": lambda: pauses(args.redo), "wake": lambda: wake(args.redo),
               "speakers": lambda: speakers(args.redo), "headphones": lambda: headphones(args.redo),
               "room": lambda: room(args.redo), "analyse": analyse}
    install_signal_handlers()
    # A sleeping Mac records nothing and stalls the unattended analysis, so it stays awake while the session runs.
    awake = subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])
    try:
        for step in steps:
            actions[step]()
    finally:
        stop(awake)
    say("Session complete. The results block is in docs/notes/phase-0-spikes.md under ## Audio.")


if __name__ == "__main__":
    main()
