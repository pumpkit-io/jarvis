# Voices are packs chosen in configuration, made with `./jarvis voice`

Jarvis is open source, but its voice was fixed in code: `QWEN3_VOICES` in `tts.py` named the two reference clips, their transcripts and trims measured by hand with ffmpeg (ADR 0032). The clips came from a script outside the repo. Anyone who wanted another voice had to rerun that script, edit Python and measure loudness themselves.

A voice is now a **voice pack**: a folder with `reference.wav` and a `voice.toml` holding its language, exact transcript, trim and, optionally, the description and model it was made with. The shipped packs are `assets/voice/jarvis-en` and `assets/voice/jarvis-it`, the same clips and trims as before. The User's packs live in the main checkout's `.jarvis/voices/`, beside the `.env` that chooses them, so every worktree finds them, and they win a name clash. `JARVIS_VOICE_EN` and `JARVIS_VOICE_IT` name the pack each language speaks with. The engine checks each pack, and that it speaks the language it was chosen for, when it loads. A broken choice leaves speech off with a message naming the setting, as a missing clip did before.

`./jarvis voice` makes and chooses packs. `design` generates candidate takes with Qwen3-TTS VoiceDesign 1.7B from a description plus a native-accent line, the workflow the original voices were made with. `try` speaks typical replies in a take or a pack through Jarvis's own 0.6B cloning path and level. `use` turns a take into a pack, and `add` does the same for any recording with its transcript. Both measure the trim and set the language's setting in `.env`. `list` shows the packs and the active ones.

The trim is measured, not hand-set: the clone speaks four sample replies, ITU-R BS.1770 integrated loudness is taken over them at 48 kHz, and the trim brings it to `SPEECH_LUFS`. On the shipped voices it measured -1.65 to -2.32 dB for English over five runs, around ADR 0032's -2.0 dB. Italian measured -4.58 to -4.74 dB over three runs, 0.3 to 0.4 dB below ADR 0032's -4.3 dB every time; these sample replies are not the five sentences ffmpeg's `ebur128` measured then. Cloning is not deterministic, so two measurements of one voice differ by a few tenths of a dB. The shipped trims stay as ADR 0032 measured them.

Languages stay English and Italian. A pack in another language is refused, because Jarvis's language detection, lexicons and replies cover only those two.

The User decided this on 2026-11-27.

## Considered Options

- Keeping voices in code and documenting how to edit `QWEN3_VOICES`. Rejected: changing a voice should not need a code change, and loudness should not need ffmpeg and judgement.
- User packs in each checkout's own `.jarvis/`. Rejected: `.env` is always read from the main checkout, so a voice chosen from a worktree would be missing in the main checkout.
- Auditioning with the 1.7B Base model, as the original script did. Rejected: Jarvis speaks through the 0.6B 8-bit model, and a voice should be judged as Jarvis will say it.
- Measuring the reference clip's loudness instead of the clone's. Rejected: the trim applies to what the clone outputs, so it is measured there, as ADR 0032 did.
