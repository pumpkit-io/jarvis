# Checkout-independent speech setup

Ticket #28, checked on 2026-11-05 on Apple Silicon macOS. Dependencies remain locked: kokoro-mlx 0.1.4, misaki 0.9.4, phonemizer-fork 3.3.2 and espeakng-loader 0.2.4. No package or model replacement was needed.

A fresh `uv sync --locked` in the ticket worktree reproduced the native failure. Its bundled eSpeak data path was 167 bytes; the standalone audio environment's path was 180 bytes. Calling Misaki's Italian G2P directly exited the Python process with a missing `phontab` at the wheel builder's `/Users/runner/...` location. The installed data was present. This establishes a setup failure before synthesis, rather than a synthesis-quality result.

[eSpeak's native resolver](https://github.com/espeak-ng/espeak-ng/blob/master/src/libespeak-ng/speech.c) uses a fixed path buffer; [the upstream path-length report](https://github.com/espeak-ng/espeak-ng/issues/1004) describes the 160-byte non-Windows limit and appended data-directory name. The runtime now validates the bundled resources before native initialization. Paths of 140 bytes or more get a private short copy of the installed data in `/tmp/jarvis-espeak-*`. Misaki imports first, then its wrapper receives the prepared path before any backend initializes. This avoids its import-time path assignment undoing the setup. The temporary copy belongs to the process and is removed when Python cleans it up; abrupt termination can leave a temporary directory.

The setup uses the current environment's wheel. It needs no symlink, environment override, data from another worktree or Homebrew installation. Short paths use the installed data directly. Existing standalone TTS, memory, AEC and session benchmarks call the same preparation function. Missing resources and copy failures raise an actionable Python error before entering the native initializer.

## Verification

`uv run python spikes/audio/check_tts_setup.py` synthesizes two fixed public probes, with no playback, microphone, agent call or recorded conversation. The standalone equivalent is `uv run --project spikes/audio python spikes/audio/check_tts_setup.py` from the repo root. First use may download Kokoro.

| Fresh environment | English `am_liam` | Italian `im_nicola` |
|---|---:|---:|
| Long worktree, runtime environment | 62,400 finite samples | 69,600 finite samples |
| Long worktree, standalone spike environment | 62,400 finite samples | 69,600 finite samples |
| Short `/tmp` checkout, runtime environment | 62,400 finite samples | 69,600 finite samples |

Each probe produced nonempty finite PCM at 24 kHz. The short checkout contained its own source and locked environment, rather than a linked environment. These are setup and generation checks, not subjective voice-quality or physical playback acceptance. Existing synthesis timing and quality measurements remain historical evidence. The prior workstation symlink workaround and macOS voice-processing initialization failure remain historical findings; this repair does not claim a new macOS voice-processing comparison.

Default regression tests use synthetic resource trees and injected setup dependencies. They check preservation of installed resources across a long path, direct use of a short path and actionable missing-data failures without native engines or devices.
