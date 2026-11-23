# Jarvis's voice

Qwen3-TTS clones these two clips (ADR 0029). Jarvis speaks English with `jarvis-en.wav` and Italian with `jarvis-it.wav`. Each clip's exact transcript lives next to its path in `QWEN3_VOICES` in `src/jarvis_hub/voice/tts.py`. Cloning needs the transcript word for word.

To change a voice, replace its WAV and update its transcript in `QWEN3_VOICES`. Keep the clip about 10 seconds long, clean, in a neutral tone, and spoken by a native speaker of its language. Any sample rate works, because the model resamples the clip. Then run `uv run python spikes/audio/check_tts_setup.py qwen3`, and listen to a few replies in both languages before keeping it.
