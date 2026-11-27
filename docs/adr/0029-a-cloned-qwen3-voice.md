# Jarvis speaks with a cloned Qwen3-TTS voice, one reference clip per language, streamed in pieces

Kokoro's voices are few and weak, the Italian one most of all. Jarvis now speaks with Qwen3-TTS 0.6B Base, which clones a short reference clip with its exact transcript. There is one clip for English and one for Italian, and each sentence is spoken with the clip of the language it is in. The synthesizer outputs 24 kHz mono float32, the same format Kokoro produced, so the iPhone path in ADR 0025 is unchanged. Kokoro remains an engine the User can select in configuration, but Jarvis never switches to it on its own: if Qwen fails to load, speech is off and the message says why.

The feasibility spike missed the bar the User set, a short first sentence in under about 600 ms, as long as each sentence was synthesised whole. On the M3 Pro a short sentence took 1.4 to 1.5 s with the bf16 checkpoint and 1.1 to 1.3 s with the 8-bit one, against Kokoro's 0.1 s. With bf16 the audio stack took 4.24 GB, over its 4 GB budget; with 8-bit it took 3.75 GB. Streaming met the bar. On 2026-11-22 the User chose the 8-bit checkpoint and streaming: the engine yields each sentence in pieces of about half a second, the first ready in about 0.4 s, and Speech plays each piece as a clip of its own while the next one is synthesised. Later pieces arrive faster than they play, so no gaps open between them.

A piece carries the words it is estimated to say, from the voices' measured speaking rate, so a Barge-in still knows roughly what was heard. The output is scaled by a fixed 2 dB cut to Kokoro's level, which the speech gate, barge-in thresholds and echo measurements were tuned on. The measurements are in `docs/notes/phase-0-spikes.md` and `spikes/audio/results/`.

The User decided this on 2026-11-22.

Since ADR 0032, each cloned voice has its own fixed trim to one speech reference level, Kokoro's English level of -25 LUFS: -2 dB for English, as before, and -4.3 dB for Italian, whose reference clip is louder.

Since ADR 0035, the clips, transcripts and trims are voice packs in `assets/voice/`, chosen with `JARVIS_VOICE_EN` and `JARVIS_VOICE_IT`, and `./jarvis voice` makes new ones.

## Considered Options

- One clip cloned into both languages. Rejected: the English accent carried into the Italian.
- Whole-sentence synthesis with the 8-bit checkpoint. Rejected: Jarvis would start speaking about a second later than with Kokoro.
- The bf16 checkpoint. Rejected: slower, and over the memory budget.
- Qwen3-TTS 1.7B. Not measured. The User chose 0.6B and would reconsider only if its voice quality falls short.
- Keeping Kokoro as the default. Rejected on voice quality. It stays as the fallback engine.
