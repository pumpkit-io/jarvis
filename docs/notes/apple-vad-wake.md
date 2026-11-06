# Opt-in Apple VAD idle wake

`JARVIS_WAKE_ENGINE=apple-vad` selects an experimental local idle detector. The default remains `openwakeword`. This is partial implementation of #37; independent confirmation and physical Hub checks remain open, along with #26.

The route uses the existing CPU Silero VAD observations to retain an idle utterance, then recognizes it with Apple SpeechTranscriber `.transcription`, always `en_US`. It accepts an exact full-word `Jarvis` anywhere in the recognition, with optional adjacent Hey/Hi/Ehi. It performs no homophone repair or label-dependent locale selection. This is keyword detection, not contextual interruption assessment. During replies, Announcements and Follow-up windows this route grants no wake authority; the separate conversation policy handles those states.

## Setup

Requires Apple Silicon, macOS 26 or later, and Swift command-line tools with the macOS 26 SDK. The native bridge additionally checks SpeechTranscriber hardware availability. Its source-hashed executable is cached under `~/Library/Caches/jarvis/native/`; text mode and the default detector do not build or start it.

Check installed English (US) assets without opening audio devices or downloading:

```sh
uv run python -m jarvis_hub.voice.apple_wake --check
```

If assets are missing, explicitly install them:

```sh
uv run python -m jarvis_hub.voice.apple_wake --install
```

Installation never evicts other language assets to make room. Resolve a reported reservation-capacity problem before retrying. No account or speech-recognition permission request is part of this setup. Normal startup never downloads Apple assets. Unsupported hardware, missing tools/assets or native failure displays a repair action; there is no silent fallback to another detector.

After completing setup, set `JARVIS_WAKE_ENGINE=apple-vad` in the checkout's `.env`. Start `./jarvis --debug` only when ready to use the microphone and speakers. `./jarvis --headphones --debug` preserves this detector and switches echo cancellation off. These launch commands also start ordinary model warm-up; setup/check commands do not.

[SpeechTranscriber availability](https://developer.apple.com/documentation/speech/speechtranscriber) and [AssetInventory](https://developer.apple.com/documentation/speech/assetinventory) describe Apple's managed assets. Apple does not expose an immutable model-weight revision here; record OS/SDK versions and installed status with measurements. The bridge links the system Speech framework and distributes no Apple model assets. Apple platform/SDK terms apply; this note grants no separate model redistribution license. The bundled Silero notice identifies its model as MIT and its openWakeWord wrapper as Apache-2.0; the legacy hey_jarvis model's non-commercial restriction remains documented in the existing engine.

## Capture and failure behavior

Idle segmentation uses fixed 512-sample windows at 16 kHz, VAD threshold 0.5, a 0.2-second onset lead where available, 0.5 seconds of trailing silence and a 0.2-second detected speech span. Conversational endpoint settings do not tune this experimental operating point. Native recognition runs on a separate thread, with at most one active and one queued utterance, at most two retained recognition candidates including completed results, a ten-second per-process deadline and at most 42 seconds of retained audio per pending utterance. A 30-second continuous utterance, queue saturation, retention loss or capture overflow rejects the candidate instead of admitting truncated audio. Failed native recognition disables this route until restart and shows the setup check.

Echo cancellation continues on source callbacks while recognition waits. The research replay used a multi-file process; this runtime bounds each utterance in a fresh process, so final Hub setup/duty costs remain unmeasured. Each native process recognizes a temporary local WAV and is reaped; temporary files are removed. Recognition text is used only for keyword and phrase-only control metadata. Apple documents that [SpeechAnalyzer transcriber modules do not send voice audio to Apple's servers](https://developer.apple.com/documentation/speech/asking-permission-to-use-speech-recognition). No cloud recognition route is used here.

Only the current idle owner may claim a proposal. Typed replacement, playback/Announcement eligibility changes, audio loss and shutdown invalidate old results. Acceptance hands retained AEC-processed source audio, including speech captured while recognition waited, to the ordinary speaker gate, STT and endpoint fence. It does not send the wake recognizer's transcript to the Hub or select Tasks. Candidate request audio goes through ordinary STT once with no wake-time cutoff, preserving fast request words instead of applying the legacy 80 ms token slack. Existing transcript phrase handling removes an opening wake phrase. If Apple recognized only the wake phrase and no later VAD speech arrived, the proposal opens listening without STT or a Talker turn; this avoids making a request from a later STT mishearing of the wake name. Speech added while recognition waited still goes through the retained-audio path. Phrase-only control depends on the recognized content: omitted words from the same utterance can still be missed, so independent live one-breath checks remain open.

Source onset/speech-end stamps remain separate from recognition delivery. Delivery starts the listening/chime windows; original stamps retain request/reply ownership and latency evidence. Whole-utterance recognition can delay a chime until the same-breath request has ended. A delayed confirmation is not evidence that physical latency targets passed.

## Evidence and remaining checks

An exploratory EOF-corrected replay of already-used recordings recovered Hey 20/20, Hi 19/20, Ehi 19/20 and bare 20/20, with zero observed false accepts over 3900 seconds of negative input. A finite-file 0.5-second flush had left twelve spans unfinished; draining the unchanged segmenter to quiescence completed them. All 676 original waveform hashes/frame boundaries matched, their decodes were reused, and only twelve new tails were recognized. All 93 files drained within 0.70 seconds; added zeros were excluded from acoustic exposure. The corrected result reaches the numeric thresholds on used data, not independent confirmation.

That replay produced 607 negative recognition jobs, about 560 per background hour. Client-only resource measurements exclude Apple's model service, so whole-Hub memory, idle CPU and readiness remain unverified. Runtime cap rejection, AEC-processed input and actual device scheduling still require validation; an offline file replay is not a measured live runtime.

Fake Capture/Hub tests cover delayed confirmation beyond pre-roll, complete request transfer, phrase-only listening, original-time reply ownership, stale owners, typed replacement, playback eligibility, loss/recovery, shutdown, bounded rejection and fail-closed native process behavior. No microphone, speakers, real native inference or asset installation runs in those tests.

Before selecting this as the default, freeze the final configuration, independently record 20 new takes for each of the four phrases and a new negative hour, and jointly confirm at least 19/20 each with at most one false wake/hour. Verify live one-breath onset, chime suppression, Follow-up behavior, headphones, idle model-turn counts, whole-Hub resources/readiness and physical meaningful-response latency. Preserve all failures and exclusions. Do not close #37 or #26 from the exploratory result or fake checks.
