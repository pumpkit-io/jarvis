# Jarvis's voice

Jarvis speaks with Qwen3-TTS, which clones a short reference clip (ADR 0029). Each language speaks with one **voice pack**: a folder holding `reference.wav` and a `voice.toml` with the clip's language, its exact transcript and the trim that brings the clone to Jarvis's speech level (ADR 0035). The two in this folder are the defaults, `jarvis-en` and `jarvis-it`.

You can give Jarvis a voice of your own, in English or Italian, without touching code. Everything below runs from the checkout and stays on your Mac.

## Design a voice from a description

```bash
./jarvis voice design --lang en --takes 5 --describe "A warm, low female voice, unhurried, with a dry sense of humour."
```

Qwen3-TTS VoiceDesign (`mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16`, about 4 GB, downloaded on first use) invents a new speaker for each take. Each take reads the language's reference sentence and plays as soon as it is ready. A native-accent line for the language is added to your description. Without `--describe` you get the description the default voices were made from. Takes land in `.jarvis/voice-takes/`, each WAV with a JSON record of its language and transcript. Add `--no-play` to only save them, and `--text "…"` to have the takes read a sentence of your own.

Pick the take you like, then hear it say typical replies through the same model and level Jarvis uses:

```bash
./jarvis voice try .jarvis/voice-takes/take_20261127-220000_en_03.wav
```

Make it Jarvis's voice:

```bash
./jarvis voice use .jarvis/voice-takes/take_20261127-220000_en_03.wav --name my-butler
```

`use` copies the take into `.jarvis/voices/my-butler/`, measures the clone's loudness to set its trim, and sets `JARVIS_VOICE_EN=my-butler` in `.env`. Restart Jarvis to hear it.

For a voice that sounds like the same person in both languages, design English and Italian takes from the same description, pick one of each that match, and `use` both.

## Use a recording

```bash
./jarvis voice add me.wav --lang it --transcript "Exactly what the clip says, word for word." --name la-mia-voce
```

The clip should be about ten seconds of one person speaking clean, neutral speech in that language, with no music or noise. Any sample rate works. The transcript must match the clip word for word, or the clone drifts. Clone only a voice you have the right to use: your own, or one whose owner agreed.

## See and switch voices

```bash
./jarvis voice list
./jarvis voice try jarvis-it
```

`list` shows every pack, marks the one each language speaks, and says why a broken pack cannot be used. To switch, set `JARVIS_VOICE_EN` or `JARVIS_VOICE_IT` in `.env` to a pack's name and restart. If the chosen pack is missing or broken, Jarvis starts with speech off and says why.

## Share a voice

Your packs live in `.jarvis/voices/`, which git ignores. To ship one with Jarvis, copy its folder here, into `assets/voice/`, and open a pull request. A pack in `.jarvis/voices/` wins over one here with the same name.

## The voice.toml format

```toml
language = "en"            # en or it
transcript = "Of course. I've checked the three repositories, …"
trim_db = -2.0             # gain that brings the clone to -25 LUFS; `use` and `add` measure it
description = "A calm, …"  # optional: the VoiceDesign description it came from
model = "mlx-community/…"  # optional: the model that made the clip
```

After editing a pack by hand, run `uv run python spikes/audio/check_tts_setup.py qwen3` to check that both configured voices load and stream in time.
