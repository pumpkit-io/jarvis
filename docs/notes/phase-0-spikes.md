# Phase 0 spikes

Results of the Phase 0 spikes for the voice Hub (Spec #3). Each section belongs to one Ticket: `## Agent SDK` to #4, `## Audio` to #5. The throwaway scripts live under `spikes/`, never in Hub code.

## Audio

Ticket #5, first pass run on 2026-11-03 on the User's MacBook Pro (Apple M3 Pro, 18 GB, macOS 27.0.1), without the User. Every number here comes from synthetic speech: macOS `say` voices reading the same 30 clips and 40 Wake word takes the User will record (`spikes/audio/corpus.py`), plus public audiobooks for false accepts. Synthetic speech is clean and evenly paced, so it proves the pipelines and gives speed and memory, but **accuracy on the User's voice, Wake word recall, echo on the real speakers and Smart Turn on real pauses wait on the recording session**. Leaders named below are provisional unless the criterion doesn't depend on the User's voice.

**Versions:** Python 3.12.10 via `uv` (3.12 because `kokoro-mlx` requires `<3.13`), `parakeet-mlx` 0.6.0, `mlx` 0.32.4, `mlx-whisper` 0.4.3, `kokoro-mlx` 0.1.4 with `misaki` 0.9.4, `openwakeword` 0.6.0, `onnxruntime` 1.31.1, `livekit` 1.2.0 (WebRTC APM), `transformers` 5.22.1, `lingua-language-detector` 2.2.0. VoiceMode's services: whisper.cpp v1.9.5 server on :2022 running `ggml-small.bin`, Kokoro-FastAPI 0.3.0 on :8880. Models: `mlx-community/parakeet-tdt-0.6b-v3`, `mlx-community/whisper-large-v3-turbo`, `ggml-large-v3-turbo.bin`, `mlx-community/Kokoro-82M-bf16`, openWakeWord `hey_jarvis_v0.1`, `pipecat-ai/smart-turn-v3` (`v3.2-cpu` and `v3.0`).

Scripts: `spikes/audio/` (`uv run <script>` from there). `make_synthetic.py` renders the corpus; `bench_stt.py`, `bench_lang.py`, `bench_tts.py`, `bench_wake.py`, `bench_aec.py`, `bench_smart_turn.py`, `bench_memory.py` measure; `session.py` is the recording session; `report.py` appends its results here. Raw JSON goes to `spikes/audio/data/results/` (gitignored).

### Verdicts

| Criterion | Result |
|---|---|
| Recording session | **Kit ready, waiting on the User.** `cd spikes/audio && uv run session.py`, about 35 minutes with the User (25 without the #4 baseline step), then about 12 minutes of unattended analysis. |
| STT latency, partials, memory | **Measured.** `parakeet-mlx` is 10 to 25 times faster than either Whisper and the only engine fast enough for 0.4 to 0.8 s partials. |
| STT word error rate on the User's clips | **Waiting on the User.** Synthetic WER: parakeet 2.2 %, mlx-whisper turbo 2.2 %, whisper.cpp turbo 2.9 %, VoiceMode's small 7.6 %. Provisional leader: `parakeet-mlx`. |
| TTS | **Chosen: `kokoro-mlx` in-process.** First audio 222 ms (EN) and 240 ms (IT) against 358 and 371 ms for VoiceMode's server; `am_liam` and `im_nicola` present in both; speed 1.0. Doesn't depend on the User's voice. |
| Echo cancellation | **Pipeline proven, acoustic run waiting on the User.** On digital echo the WebRTC APM takes VAD speech frames from 87 % to under 0.5 % and keeps 3 of 3 Barge-ins. The Voice Processing I/O helper is built for the session. |
| Wake word recall | **Waiting on the User.** Synthetic: "Hey Jarvis" and "Hi Jarvis" 10/10, "Jarvis" 7/10, "Ehi Jarvis" 6/10 at threshold 0.5. The last two are at risk. |
| Wake word false accepts per hour | **Measured on public audio: 0 per hour at threshold 0.5** over 11.5 h of English and Italian audiobooks; 0.8 per hour at 0.3. A room run with a video playing is in the session. |
| Wake word licence | openWakeWord code is Apache-2.0; the pretrained `hey_jarvis` model is CC BY-NC-SA 4.0 (non-commercial: fine here, goes in the README). Porcupine not run: it needs a Picovoice AccessKey, which needs an account. |
| Smart Turn v3 | **Inference measured: 14.8 ms p50** (v3.2 cpu, 8.3 MB). Saves about 485 ms per complete turn against VoiceMode's 1000 ms silence threshold; whether it holds on the User's pauses waits on the session. |
| Memory | **Within budget.** Whole audio stack in one process 2.4 GB (parakeet) or 2.8 GB (mlx-whisper); with three idle `claude` processes 3.3 or 3.65 GB, against 4 GB. Needs an MLX cache cap. |
| Chosen STT, echo cancellation, Wake word | **Waiting on the User.** |

### STT

30 clips (15 EN, 15 IT; 20 short of 0.8 to 2.2 s, 10 long of 5.6 to 6.3 s), p50 of three runs after a warm-up. "2 s" is the p50 over short clips, "6 s" over long ones. Partials are the first 0.4, 0.6 and 0.8 s from speech onset of each short clip. WER is on lowercased text without punctuation, counting "Pump Kit" as "pumpkit", "42" as "forty two" and similar spellings as equal (`EQUIVALENTS` in `bench_stt.py`).

| Engine | 2 s | 6 s | Partials 0.4 / 0.6 / 0.8 s | WER all / EN / IT | Memory |
|---|---|---|---|---|---|
| `parakeet-mlx` tdt-0.6b-v3, in-process | 80 to 105 ms | 142 to 238 ms | 84 / 90 / 98 ms | 2.2 / 0.0 / 4.5 % | 1.2 GB MLX active, load 1.6 s |
| `mlx-whisper` large-v3-turbo, in-process | 1,258 ms | 1,320 ms | 1,243 / 1,234 / 1,256 ms | 2.2 / 2.7 / 1.5 % | 1.5 GB MLX active, load 5.4 s |
| whisper.cpp `ggml-small` (VoiceMode :2022) | 512 ms | 604 ms | 446 / 460 / 611 ms | 7.6 / 2.7 / 12.9 % | 0.74 GB footprint |
| whisper.cpp large-v3-turbo (spike-only :2023) | 2,155 ms | 2,255 ms | 2,139 / 2,155 / 2,146 ms | 2.9 / 1.4 / 4.5 % | 1.86 GB footprint |

Ranges for parakeet are two runs on different minutes of the same afternoon. Findings:

- **Whisper's cost is flat.** Both Whisper builds encode a 30 s window whatever the clip length, so a 0.4 s snippet costs as much as a 6 s clip. That rules Whisper out for Barge-in partials, which the Spec wants at about 400 and 700 ms into the User's speech.
- **Parakeet partials are usable.** At 0.4 s it often has the first word ("Wait.", "Cancel"); at 0.8 s the first two to four words ("What are you working on?", "Open a PR.", "Is the CI green?"). Enough for the hard-interrupt and Backchannel lexicons.
- **Parakeet reports no language.** The Hub picks the reply voice from the User's language, so with parakeet it needs text language detection. `lingua` restricted to EN and IT got 28/30 on parakeet's transcripts in 0.03 ms; both misses were short, mixed clips ("Merge pull request 42.", "La C verde su Pump Kit."). The Hub should keep the previous language when detection isn't confident. Whisper turbo's own detection got 30/30.
- **VoiceMode's server translates Italian unless asked not to.** whisper.cpp's server defaults to English when the request names no language, and returned English translations of the Italian clips (IT WER 96 %). The numbers above pass `language=auto`.
- The large-v3-turbo whisper.cpp server ran on :2023 from VoiceMode's binary and a downloaded model in `~/.cache/jarvis-spikes/whisper/`, and was stopped afterwards; VoiceMode's own config was not touched.

### TTS

The 12-word sentences in `corpus.py` (`TTS_SENTENCES`), speed 1.0, p50 of five runs after a warm-up.

| Engine | First audio EN (`am_liam`) | First audio IT (`im_nicola`) | Memory |
|---|---|---|---|
| `kokoro-mlx` in-process, `generate_stream` | 222 ms | 240 ms | 313 MB MLX active, load 0.6 s |
| VoiceMode Kokoro-FastAPI :8880, streamed PCM | 358 ms | 371 ms | 2.0 GB footprint |

- `kokoro-mlx` yields one chunk per sentence, so first audio is the whole sentence. That fits the Hub, which synthesises sentence by sentence anyway.
- Both voices exist in both engines; `kokoro-mlx` picks Italian G2P (espeak) from the `im_` prefix. Its output transcribed back through parakeet as the intended sentences.
- **VoiceMode's server restarts itself every 25 requests**: its launchd plist sets `UVICORN_LIMIT_MAX_REQUESTS=25`. The benchmark tripped it once; the first request after a restart took 0.83 to 0.90 s. In a Hub that sends one request per sentence, that would be a silent gap every few replies.
- `misaki` downloads its spaCy model with `pip` into whatever virtualenv is active, and did so into another repo's venv during this spike (reverted). The Hub must declare `en-core-web-sm` as a dependency, as `spikes/audio/pyproject.toml` now does.

### Echo cancellation

`bench_aec.py simulate` plays Jarvis's lines (Kokoro, EN and IT) through a toy room (35 ms delay plus a decaying tail), adds noise, overlays three User clips in the second half, and runs Silero VAD (512-sample frames, p > 0.5) on the raw mic and on the WebRTC APM output (echo cancellation, noise suppression and high-pass on, 10 ms frames, reference through `process_reverse_stream`).

| Path | VAD speech frames during Jarvis alone | User Barge-ins kept |
|---|---|---|
| Raw mic | 86.8 % | 3/3 |
| WebRTC APM, stream delay 0 to 40 ms | 0.0 % | 3/3 |
| WebRTC APM, stream delay 60 to 120 ms | 0.1 to 0.5 % | 3/3 |

Digital echo is linear and easy, so this only proves the pipeline. The session's speaker run measures the real thing: about a minute of Jarvis on the built-in speakers recorded with a sample-aligned duplex stream, the User silent for the first half and speaking five cued Backchannels and Interruptions in the second. The same run then repeats through macOS Voice Processing I/O (`vpio_playrec.swift`, compiled by the session; typechecked here but not run, since it plays aloud and needs the mic), so both options are measured in one sitting. `bench_aec.py analyse` reports Barge-in triggers (VAD speech held 200 ms) per minute on Jarvis alone and the User Barge-ins detected, for the raw mic, WebRTC at six stream delays and VPIO. Headphones mode needs none of this: with no echo path the raw mic is the input.

### Wake word

openWakeWord `hey_jarvis_v0.1`, ONNX, 80 ms frames; a take counts when its peak score reaches the threshold. Synthetic takes: 10 per phrasing from nine `say` voices at random rates (Italian voices for "Ehi Jarvis").

| Phrasing | @0.3 | @0.5 | @0.7 | Median peak |
|---|---|---|---|---|
| Hey Jarvis | 10/10 | 10/10 | 10/10 | 0.998 |
| Hi Jarvis | 10/10 | 10/10 | 10/10 | 0.998 |
| Ehi Jarvis | 7/10 | 6/10 | 4/10 | 0.674 |
| Jarvis | 8/10 | 7/10 | 7/10 | 0.967 |

`vad_threshold=0.5` changed nothing on these clean takes. If the User's "Ehi Jarvis" and bare "Jarvis" also fall short of 95 %, the Ticket's open question applies: Porcupine's built-in `jarvis` keyword (matches "…Jarvis" in any phrase, needs a free AccessKey and an account) or a custom openWakeWord model trained on synthetic "Ehi Jarvis" and "Jarvis" clips (no account, a few hours of training).

#### False accepts

11.5 hours of read speech that never says "Jarvis", streamed through the model in 80 ms frames, one detection per 2 s:

| Audio | Hours | @0.3 | @0.5 | @0.7 |
|---|---|---|---|---|
| LibriSpeech `dev-clean` (English, many speakers) | 5.39 | 0 / 0 | 0 / 0 | 0 / 0 |
| LibriVox *Il giornalino di Gian Burrasca* (Italian, one reader) | 6.15 | 5 / 3 | 0 / 0 | 0 / 0 |

Each cell is false accepts without / with `vad_threshold=0.5`. At threshold 0.5 that is 0 per hour over 11.5 hours; at 0.3 it is 0.8 per hour on Italian (0.5 with VAD), still under the Spec's 1 per hour. Model cost is 3.4 to 3.8 % of one core in real time. Read audiobooks are cleaner than a room with a video playing, so the session also records 5 minutes of that, and the User's own speech near "Jarvis" is untested.

### Smart Turn v3

ONNX on CPU, one inter-op thread, including Whisper feature extraction; audio is the last 8 s with zeros padded at the start, as Pipecat does (padding at the end, which the upstream sample script does, flips most verdicts to "complete"). Complete turns are the 30 clips; cut-off turns are the 10 long clips cut at the quietest word gap near their middle.

| Model | Inference p50 (max) | Complete judged complete | Cut-off judged incomplete |
|---|---|---|---|
| `smart-turn-v3.2-cpu.onnx` (8.3 MB) | 14.8 ms (26 ms) | 28/30 | 6/10 |
| `smart-turn-v3.0.onnx` (8.4 MB) | 14.6 ms (28 ms) | 24/30 | 8/10 |

With the Spec's 500 ms VAD silence, a complete turn ends 515 ms after the User stops, against 1000 ms for VoiceMode's fixed silence threshold today: about 485 ms saved per turn (285 ms against 800 ms, 985 ms against 1500 ms). The cost is a cut-off turn judged complete, which ends the User's turn early; 6/10 on synthetic cuts is too few to judge, and the User's own clips (with real pauses) decide it.

### Memory

`bench_memory.py`: one process loads the STT, `kokoro-mlx` with both voices, openWakeWord with Silero VAD, Smart Turn v3.2 and the WebRTC APM, runs each, then reuses STT and TTS three times. MLX's buffer cache is capped at 256 MB; uncapped, the same process reaches 3.9 to 4.8 GB footprint from freed buffers alone, so the Hub must call `mx.set_cache_limit`. The `claude` share is the median footprint of the six `claude` processes running at the time (291 MB), times three.

| Stack | Audio stack footprint | MLX peak during load | Plus three idle `claude` |
|---|---|---|---|
| parakeet STT | 2.42 GB | 2.77 GB | 3.30 GB |
| mlx-whisper STT | 2.78 GB | 3.10 GB | 3.65 GB |

Both are under the 4 GB audio-stack budget on the 18 GB machine. Using VoiceMode's servers instead would add their own processes (0.74 GB for whisper small, 1.86 GB for turbo, 2.0 GB for Kokoro).

### The recording session

`cd spikes/audio && uv run session.py` runs these steps in order; `--only <step>` runs one, and every step skips takes already on disk, so it can stop and resume. Recordings go to `spikes/audio/data/recordings/` (gitignored).

1. **Preflight** (2 min): built-in mic and speakers, a 3 s level check, and a stop if macOS gives the terminal no mic access.
2. **Baseline, Ticket #4** (about 10 min): a placeholder that points at #4's hook-based harness on `voice-hub-t4` (`spikes/agent/baseline/`, steps in that branch's `## Agent SDK` notes) and waits.
3. **Clips** (about 10 min): the 30 sentences, Enter to start and stop, keep or redo.
4. **Wake word** (about 3 min): "Hey Jarvis", "Hi Jarvis", "Ehi Jarvis", "Jarvis", ten 2.5 s takes each on a cue.
5. **Speaker run** (about 9 min): the WebRTC pass, the Voice Processing I/O pass, then 5 minutes of the room with a video or podcast playing, for false accepts.
6. **Analysis** (about 12 min, unattended; a dry run on the synthetic corpus took 11.7 min): starts a temporary large-v3-turbo server on :2023, runs every bench on the recordings, stops the server, and appends a results block to this section with `report.py`.
