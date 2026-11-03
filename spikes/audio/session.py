"""The User's recording session for Ticket #5, then every measurement on what it recorded.

    cd spikes/audio && uv run session.py              # the whole session, about 30 minutes
    uv run session.py --only clips                     # one step: preflight, baseline, clips, wake, speakers, analyse
    uv run session.py --redo                           # re-record takes that already exist

Recordings go to data/recordings/ (gitignored). Each step skips takes already on disk,
so a session can stop and resume. The last step appends results to the notes.
"""

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

from common import DATA, HERE, RECORDINGS, SR, resample, save_16k
from corpus import CLIPS, JARVIS_LINES, TAKES_PER_PHRASING, WAKE_PHRASINGS

PLAY_SR = 48_000
WAKE_TAKE_S = 2.5
VIDEO_MINUTES = 5
TURBO_MODEL = Path.home() / ".cache/jarvis-spikes/whisper/ggml-large-v3-turbo.bin"
WHISPER_SERVER = Path.home() / ".voicemode/services/whisper/build/bin/whisper-server"
STEPS = ["preflight", "baseline", "clips", "wake", "speakers", "analyse"]

# Where the User says something over Jarvis in the speaker run: (seconds into the second half, words).
BARGE_IN_CUES = [(3.0, "okay"), (9.0, "mhm"), (15.0, "wait, stop"), (21.0, "aspetta"), (27.0, "actually, open an issue instead")]


def say(text: str) -> None:
    print(f"\n{text}")


def wait_enter(prompt: str = "Press Enter to go on.") -> str:
    return input(f"  {prompt} ").strip().lower()


def record_until_enter() -> np.ndarray:
    chunks: list[np.ndarray] = []
    with sd.InputStream(samplerate=SR, channels=1, dtype="float32", callback=lambda d, *_: chunks.append(d.copy())):
        input("  ● recording... press Enter when you finish speaking. ")
    time.sleep(0.05)
    return np.concatenate(chunks)[:, 0] if chunks else np.zeros(0, np.float32)


def record_seconds(seconds: float) -> np.ndarray:
    audio = sd.rec(int(seconds * SR), samplerate=SR, channels=1, dtype="float32")
    sd.wait()
    return audio[:, 0]


def level_db(audio: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(audio**2)) + 1e-9))


def preflight() -> None:
    say("Preflight. Use the MacBook's built-in microphone and speakers; unplug headphones.")
    print(f"  input:  {sd.query_devices(kind='input')['name']}")
    print(f"  output: {sd.query_devices(kind='output')['name']}")
    wait_enter("Say a few words after pressing Enter (3 s).")
    audio = record_seconds(3)
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


def wake(redo: bool) -> None:
    say(f"Wake word: each phrasing {TAKES_PER_PHRASING} times. Say it when the dot appears; vary tone and distance a little.")
    for key, phrase in WAKE_PHRASINGS.items():
        todo = [t for t in range(1, TAKES_PER_PHRASING + 1)
                if redo or not (RECORDINGS / "wake" / f"{key}_{t:02d}.wav").exists()]
        if not todo:
            continue
        wait_enter(f'Next: "{phrase}". Enter to start.')
        for take in todo:
            print(f'  ● "{phrase}"  ({take}/{TAKES_PER_PHRASING})', flush=True)
            save_16k(RECORDINGS / "wake" / f"{key}_{take:02d}.wav", record_seconds(WAKE_TAKE_S))
            time.sleep(0.4)


def jarvis_reference() -> np.ndarray:
    path = RECORDINGS / "speaker-run" / "reference_48k.wav"
    if path.exists():
        import soundfile as sf

        return sf.read(path, dtype="float32")[0]
    print("  synthesising Jarvis's lines with kokoro-mlx...")
    from kokoro_mlx import KokoroTTS

    tts = KokoroTTS.from_pretrained("mlx-community/Kokoro-82M-bf16")
    parts = []
    for lang, line in JARVIS_LINES:
        audio = np.asarray(tts.generate(line, voice="am_liam" if lang == "en" else "im_nicola").audio, np.float32)
        parts += [resample(audio, tts.SAMPLE_RATE, PLAY_SR), np.zeros(PLAY_SR // 2, np.float32)]
    half = np.concatenate(parts)
    ref = np.concatenate([half, half]) * 0.8
    path.parent.mkdir(parents=True, exist_ok=True)
    import soundfile as sf

    sf.write(path, ref, PLAY_SR)
    return ref


def show_cues(start: float, offset: float) -> list[list[float]]:
    spans = []
    for at, words in BARGE_IN_CUES:
        delay = start + offset + at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        print(f'  ● say: "{words}"', flush=True)
        t = time.monotonic() - start
        spans.append([round(t, 2), round(t + 2.0, 2)])
    return spans


def speakers(redo: bool) -> None:
    run = RECORDINGS / "speaker-run"
    say("Speaker run. Built-in speakers at your usual volume, no headphones. Jarvis talks for about a minute.\n"
        "Stay silent for the first half. In the second half, say the words shown, over Jarvis.")
    ref = jarvis_reference()
    half_s = len(ref) / PLAY_SR / 2
    if redo or not (run / "mic.wav").exists():
        wait_enter("Enter to start (WebRTC pass: raw mic).")
        start = time.monotonic()
        rec = sd.playrec(ref, samplerate=PLAY_SR, channels=1, dtype="float32")
        spans = show_cues(start, half_s)
        sd.wait()
        save_16k(run / "mic.wav", resample(rec[:, 0], PLAY_SR, SR))
        save_16k(run / "reference.wav", resample(ref, PLAY_SR, SR))
        (run / "segments.json").write_text(json.dumps({"user_speech": spans, "cues": BARGE_IN_CUES}))
    if redo or not (run / "vpio.wav").exists():
        helper = DATA / "vpio_playrec"
        if not helper.exists():
            subprocess.run(["swiftc", "-O", str(HERE / "vpio_playrec.swift"), "-o", str(helper)], check=True)
        wait_enter("Same again, through macOS Voice Processing I/O. Enter to start.")
        start = time.monotonic()
        proc = subprocess.Popen([str(helper), str(run / "reference_48k.wav"), str(run / "vpio.wav")])
        spans: list[list[float]] = []
        cue_thread = threading.Thread(target=lambda: spans.extend(show_cues(start, half_s)))
        cue_thread.start()
        proc.wait()
        cue_thread.join()
        if proc.returncode:
            print("  Voice Processing I/O run failed; the WebRTC numbers still stand.")
        segments = json.loads((run / "segments.json").read_text())
        (run / "segments.json").write_text(json.dumps(segments | {"user_speech_vpio": spans}))
    if redo or not (run / "video.wav").exists():
        say(f"Now start a video or podcast with people talking on this Mac, at normal volume. I record {VIDEO_MINUTES} minutes.\n"
            "Stay quiet and don't say Jarvis. This measures false wakes with a video playing.")
        wait_enter("Enter once the video is playing.")
        save_16k(run / "video.wav", record_seconds(VIDEO_MINUTES * 60))
        print("  done; you can stop the video.")


def analyse() -> None:
    say("Analysis. This runs on its own for a few minutes; you can leave.")
    py = [sys.executable]
    turbo = None
    if TURBO_MODEL.exists() and WHISPER_SERVER.exists():
        turbo = subprocess.Popen([str(WHISPER_SERVER), "--host", "127.0.0.1", "--port", "2023", "--model", str(TURBO_MODEL),
                                  "--inference-path", "/v1/audio/transcriptions", "--threads", "11", "--convert"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(8)
    try:
        run = RECORDINGS / "speaker-run"
        for cmd in (["bench_stt.py", "--source", "recordings"],
                    ["bench_lang.py", "--source", "recordings"],
                    ["bench_wake.py", "recall", "--source", "recordings"],
                    ["bench_wake.py", "false-accepts", str(run / "video.wav"), "--name", "false-accepts-video"],
                    ["bench_aec.py", "analyse", str(run)],
                    ["bench_smart_turn.py", "--source", "recordings"]):
            print(f"  $ {' '.join(cmd)}")
            subprocess.run(py + [str(HERE / cmd[0])] + cmd[1:], cwd=HERE, check=False, stdout=subprocess.DEVNULL)
    finally:
        if turbo:
            turbo.terminate()
    subprocess.run(py + [str(HERE / "report.py")], cwd=HERE, check=False)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=STEPS)
    ap.add_argument("--redo", action="store_true")
    args = ap.parse_args()
    steps = [args.only] if args.only else STEPS
    actions = {"preflight": preflight, "baseline": baseline, "clips": lambda: clips(args.redo),
               "wake": lambda: wake(args.redo), "speakers": lambda: speakers(args.redo), "analyse": analyse}
    for step in steps:
        actions[step]()
    say("Session complete. Results are appended to docs/notes/phase-0-spikes.md under ## Audio.")


if __name__ == "__main__":
    main()
