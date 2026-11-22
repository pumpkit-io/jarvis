# Jarvis speaks with a cloned Qwen3-TTS voice, one reference clip per language

Kokoro's voices are few and weak, the Italian one most of all. Jarvis now speaks with Qwen3-TTS 0.6B Base, which clones a short reference clip with its exact transcript. There is one clip for English and one for Italian, and each sentence is spoken with the clip of the language it is in. The synthesizer outputs 24 kHz mono float32, the same format Kokoro produced, so the iPhone path in ADR 0025 is unchanged. Kokoro remains an engine the User can select in configuration, but Jarvis never switches to it on its own: if Qwen fails to load, speech is off and the message says why.

The decision depends on the feasibility spike. Qwen must synthesise faster than real time, keep a short first sentence under about 600 ms on the M3 Pro, and keep the audio stack under 4 GB. If it misses any of these, the User decides what to do next; nobody switches models without asking.

The User decided this on 2026-11-22.

## Considered Options

- One clip cloned into both languages. Rejected: the English accent carried into the Italian.
- Qwen3-TTS 1.7B. Rejected for now because 0.6B is lighter. We revisit 1.7B only if 0.6B fails the spike on quality, not speed.
- Keeping Kokoro as the default. Rejected on voice quality. It stays as the fallback engine.
