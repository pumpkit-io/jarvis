# Playback crackling on the MacBook

The 2026-11-04 report described loud cracks several times per second during voice replies. The launcher resolves to the main checkout at `14caa18`. Generated Kokoro samples in the diagnostic were finite, below full scale (peaks around 0.49–0.57), and had no adjacent jumps above 0.5. The device output timeline, however, lost samples during concurrent local Kokoro generation and Parakeet recognition.

## Reproduction

From the repo root, after `uv sync --locked`:

```bash
uv run python spikes/audio/bench_playback.py --old-buffer
uv run python spikes/audio/bench_playback.py
```

This opt-in probe loads and warms the local TTS/STT models before opening the default output device, then runs inference concurrently. It zeros every output block before returning it to the device: no speech is played and no microphone or Claude session is opened. Cached model downloads may still be needed. It is not part of the default test suite.

The old `latency="low"` request gave 35.375 ms actual latency and device-selected 15-sample callbacks at 24 kHz, about 1,600 Python callbacks per second. A run had a net deficit of 3,708 samples (154.5 ms) in the DAC timeline during inference. Another had approximately 8,062 positive-gap samples. PortAudio's underrun flag stayed false, so checking that flag alone missed this failure.

A fixed 20 ms callback with the old latency request still showed gaps. Requesting 100 ms latency with the driver-selected callback size gave 1,566-sample callbacks and zero positive gaps, with a net clock difference below one sample, in the diagnostic runs. The production fix changes only the requested output latency and leaves the callback size adaptive. This follows the buffering guidance in the [sounddevice stream documentation](https://python-sounddevice.readthedocs.io/en/0.5.3/api/streams.html).

The saved probe warms both models to test normal conversation rather than mixing first-load work into inference. With that probe, the old setting failed with a net 3,410.8-sample deficit (142.12 ms); the fixed setting passed with a net clock difference of -0.7 samples (-0.03 ms). A preliminary probe that deliberately loaded recognition while already playing audio still saw an 83.26 ms gap with the new buffer. This setting does not guarantee glitch-free audio during arbitrary model-loading stalls.

The deterministic regression simulates a 50 ms scheduling stall through the player/device seam. The old buffer drops the last 351 of 1,200 samples to silence; the new buffer preserves the complete output.

## Limits

The fix trades additional device buffering for tolerance of inference scheduling stalls. On this device the requested buffer increases from about 35 ms to 100 ms; playback start and interruption response need live verification with this setting. The silent probe confirms device-timeline continuity, not a listening assessment or proof against every cause of audible distortion. Full microphone/echo-cancellation behavior is not exercised by this probe.
