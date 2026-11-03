"""The User's recording session for Ticket #5, then every measurement on what it recorded.

    cd spikes/audio && uv run session.py              # the whole session, about 40 minutes
    uv run session.py --only clips                     # one step: preflight, baseline, clips, pauses, wake, speakers, analyse
    uv run session.py --redo                           # re-record takes that already exist

Recordings go to data/recordings/ (gitignored). Each step skips takes already on disk,
so a session can stop and resume. The analysis writes numbers without transcripts to
results/ (tracked) and replaces its block in the notes.
"""

import argparse
import json
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
ENTER_CLICK_S = 0.15
TURBO_MODEL = Path.home() / ".cache/jarvis-spikes/whisper/ggml-large-v3-turbo.bin"
WHISPER_SERVER = Path.home() / ".voicemode/services/whisper/build/bin/whisper-server"
TURBO_PORT = 2023
SERVER_READY_TIMEOUT_S = 120
STEPS = ["preflight", "baseline", "clips", "pauses", "wake", "speakers", "analyse"]

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
                print("  I need the pause in this one: pause a little longer.")
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


def cue_times(ref_s: float) -> list[float]:
    """Seconds after playback starts at which each Barge-in cue shows, spread over the second half of Jarvis."""
    half = (ref_s - SPEAKER_TAIL_S) / 2
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


def marks(cues: list[float], offset: float) -> dict:
    """Where the User spoke and where Jarvis was alone, in seconds of a recording whose playback began at `offset`."""
    return {"jarvis_only": [round(max(0.0, offset), 2), round(offset + cues[0], 2)],
            "user_speech": [[round(offset + c, 2), round(offset + c + USER_SPAN_S, 2)] for c in cues]}


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
    seen, timed_out = "", False
    try:
        for line in proc.stdout:
            seen += line
            if line.startswith("playing_at="):
                playing = float(line.split("=", 1)[1])
                show_cues(lambda: time.time() - playing, cues)
                break
        seen += proc.communicate(timeout=ref_s + 30)[0] or ""
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        stop(proc)
    times = parse_helper_output(seen)
    reason = "helper timed out" if timed_out else vpio_failure(proc.returncode, times, wav)
    if reason:
        wav.unlink(missing_ok=True)
        return {"failed": reason}
    return marks(cues, times["playing_at"] - times["recording_started_at"])


def compile_helper() -> Path | None:
    helper = DATA / "vpio_playrec"
    source = HERE / "vpio_playrec.swift"
    if helper.exists() and helper.stat().st_mtime >= source.stat().st_mtime:
        return helper
    print("  compiling the Voice Processing I/O helper...")
    helper.parent.mkdir(parents=True, exist_ok=True)
    if subprocess.run(["swiftc", "-O", str(source), "-o", str(helper)], capture_output=True).returncode:
        return None
    return helper


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
        start = time.monotonic()
        rec = sd.playrec(ref, samplerate=PLAY_SR, channels=1, dtype="float32")
        show_cues(lambda: time.monotonic() - start, cues)
        sd.wait()
        save_16k(run / "mic.wav", resample(rec[:, 0], PLAY_SR, SR))
        save_16k(run / "reference.wav", resample(ref, PLAY_SR, SR))
        # playrec is a duplex stream: recording and playback share sample 0.
        segments = {**marks(cues, 0.0), "cues": dict(zip(BARGE_IN_WORDS, cues)), "vpio": segments.get("vpio", {})}
        segments_path.write_text(json.dumps(segments))
    if redo or not (run / "vpio.wav").exists():
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
        print("  done; you can stop the video.")


def port_open(port: int) -> bool:
    try:
        httpx.get(f"http://127.0.0.1:{port}/", timeout=1)
        return True
    except httpx.TransportError:
        return False


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
    if port_open(TURBO_PORT):
        loud(f"Something already listens on :{TURBO_PORT}; the turbo row measures whatever that is.")
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
            if port_open(TURBO_PORT):
                return proc
            time.sleep(0.5)
        loud(f"The whisper.cpp turbo server did not answer within {SERVER_READY_TIMEOUT_S} s; no turbo row. Log: {log}")
        stop(proc)
        return None
    except BaseException:
        stop(proc)
        raise


BENCHES = [
    ["bench_stt.py", "--source", "recordings"],
    ["bench_lang.py", "--source", "recordings"],
    ["bench_wake.py", "recall", "--source", "recordings"],
    ["bench_wake.py", "false-accepts", "{run}/video.wav", "--name", "false-accepts-video"],
    ["bench_aec.py", "analyse", "{run}"],
    ["bench_smart_turn.py", "--source", "recordings"],
]


def analyse() -> None:
    say("Analysis. This runs on its own for about 12 minutes; you can leave.")
    run = RECORDINGS / "speaker-run"
    log = DATA / "analysis.log"
    failed = []
    turbo = None
    try:
        turbo = start_turbo_server()
        with log.open("w") as out:
            for cmd in BENCHES:
                cmd = [part.format(run=run) for part in cmd]
                print(f"  $ {' '.join(cmd)}", flush=True)
                code = subprocess.run([sys.executable, str(HERE / cmd[0])] + cmd[1:], cwd=HERE, check=False,
                                      stdout=out, stderr=subprocess.STDOUT).returncode
                if code:
                    failed.append(f"{' '.join(cmd)} (exit {code})")
    finally:
        stop(turbo)
    if failed:
        loud("These steps FAILED; their rows are missing. Details in " + str(log) + ":\n  " + "\n  ".join(failed))
    subprocess.run([sys.executable, str(HERE / "report.py")], cwd=HERE, check=False)
    print(f"  numbers: {RESULTS}/*-recordings.json, aec-speaker-run.json, wake-false-accepts-video.json")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=STEPS)
    ap.add_argument("--redo", action="store_true")
    args = ap.parse_args()
    steps = [args.only] if args.only else STEPS
    actions = {"preflight": preflight, "baseline": baseline, "clips": lambda: clips(args.redo),
               "pauses": lambda: pauses(args.redo), "wake": lambda: wake(args.redo),
               "speakers": lambda: speakers(args.redo), "analyse": analyse}
    for step in steps:
        actions[step]()
    say("Session complete. The results block is in docs/notes/phase-0-spikes.md under ## Audio.")


if __name__ == "__main__":
    main()
