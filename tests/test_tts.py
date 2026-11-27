from collections.abc import Iterable
from pathlib import Path

import numpy as np
import pytest

from jarvis_hub.voice.language import Lang
from jarvis_hub.voice.player import Samples
from jarvis_hub.voice.tts import (
    QWEN3_VOICES,
    TTS_ENGINES,
    ClonedVoice,
    KokoroSynthesizer,
    Qwen3Synthesizer,
)


class FakeCloner:
    """Speaks a sentence as `pieces` half-second pieces of a constant level."""

    def __init__(self, sample_rate: int = 24_000, pieces: int = 2, level: float = 0.5) -> None:
        self.sample_rate = sample_rate
        self.pieces = pieces
        self.level = level
        self.calls: list[tuple[str, ClonedVoice]] = []

    def stream(self, text: str, voice: ClonedVoice) -> Iterable[Samples]:
        self.calls.append((text, voice))
        for _ in range(self.pieces):
            yield np.full(self.sample_rate // 2, self.level, dtype=np.float32)


def voices(tmp_path: Path) -> dict[Lang, ClonedVoice]:
    for name in ("en", "it"):
        (tmp_path / f"{name}.wav").write_bytes(b"RIFF")
    return {
        "en": ClonedVoice("clone-en", tmp_path / "en.wav", "An English transcript.", "English", -2.0),
        "it": ClonedVoice("clone-it", tmp_path / "it.wav", "Una trascrizione italiana.", "Italian", -4.3),
    }


def loaded(tmp_path: Path, model: FakeCloner) -> Qwen3Synthesizer:
    synth = Qwen3Synthesizer(voices(tmp_path), lambda: model)
    synth.load()
    return synth


def test_each_language_speaks_its_own_clip_transcript_and_language(tmp_path: Path) -> None:
    model = FakeCloner()
    synth = loaded(tmp_path, model)

    list(synth.stream("Good evening, sir.", synth.voice_for("en")))
    list(synth.stream("Buonasera, signore.", synth.voice_for("it")))

    assert [(text, v.clip.name, v.transcript, v.language) for text, v in model.calls] == [
        ("Good evening, sir.", "en.wav", "An English transcript.", "English"),
        ("Buonasera, signore.", "it.wav", "Una trascrizione italiana.", "Italian"),
    ]


def test_pieces_are_24khz_mono_float32_at_kokoros_level_and_say_every_word(tmp_path: Path) -> None:
    synth = loaded(tmp_path, FakeCloner(pieces=3))
    text = "I have opened the pull request on pumpkit and checks are running."

    pieces = list(synth.stream(text, "clone-en"))

    assert synth.sample_rate == 24_000
    assert all(samples.dtype == np.float32 and samples.ndim == 1 for _, samples in pieces)
    assert sum(len(samples) for _, samples in pieces) >= 3 * 12_000
    assert pieces[0][1][0] == pytest.approx(0.397, abs=1e-3)
    assert " ".join(words for words, _ in pieces if words).split() == text.split()
    # Words go out with the audio that says them, not all with the last piece.
    assert pieces[0][0] and pieces[0][0] != " ".join(text.split())


def test_each_voice_is_trimmed_by_its_own_fixed_gain(tmp_path: Path) -> None:
    synth = loaded(tmp_path, FakeCloner(pieces=1))

    [(_, english)] = list(synth.stream("Evening.", "clone-en"))
    [(_, italian)] = list(synth.stream("Sera.", "clone-it"))

    assert np.allclose(english, 0.5 * 10 ** (-2.0 / 20))
    assert np.allclose(italian, 0.5 * 10 ** (-4.3 / 20))


def test_the_italian_clone_is_trimmed_further_than_the_english_one_to_meet_the_same_level() -> None:
    assert QWEN3_VOICES["it"].trim_db < QWEN3_VOICES["en"].trim_db


def test_words_the_audio_had_not_reached_ride_on_a_short_silent_tail(tmp_path: Path) -> None:
    synth = loaded(tmp_path, FakeCloner(pieces=1))
    text = "A sentence far longer than the half second of audio the model returned for it, sir."

    pieces = list(synth.stream(text, "clone-en"))

    assert len(pieces) == 2
    tail_words, tail = pieces[-1]
    assert tail_words and not tail.any() and len(tail) < 24_000 // 10
    assert " ".join(words for words, _ in pieces).split() == text.split()


def test_loud_audio_is_limited_rather_than_clipped_past_full_scale(tmp_path: Path) -> None:
    synth = loaded(tmp_path, FakeCloner(level=2.0))

    pieces = list(synth.stream("Loud.", "clone-en"))

    assert max(float(np.max(np.abs(samples))) for _, samples in pieces) <= 1.0


def test_a_model_at_another_rate_is_resampled_to_24khz(tmp_path: Path) -> None:
    synth = loaded(tmp_path, FakeCloner(sample_rate=48_000, pieces=1))

    pieces = list(synth.stream("One.", "clone-en"))

    assert len(pieces[0][1]) == 12_000
    assert pieces[0][1].dtype == np.float32


def test_a_missing_reference_clip_fails_load_naming_it_and_the_fallback(tmp_path: Path) -> None:
    cloned = voices(tmp_path)
    cloned["it"].clip.unlink()
    synth = Qwen3Synthesizer(cloned, lambda: FakeCloner())

    with pytest.raises(FileNotFoundError, match=r"it\.wav.*JARVIS_TTS_ENGINE=kokoro"):
        synth.load()


def test_an_unknown_voice_is_refused(tmp_path: Path) -> None:
    synth = loaded(tmp_path, FakeCloner())

    with pytest.raises(ValueError, match="am_liam"):
        list(synth.stream("Hello.", "am_liam"))


def test_the_cloned_voices_ship_with_the_checkout() -> None:
    assert {lang: voice.clip.is_file() for lang, voice in QWEN3_VOICES.items()} == {"en": True, "it": True}


def test_kokoro_speaks_american_english_and_italian_never_a_british_voice() -> None:
    kokoro = KokoroSynthesizer()
    assert (kokoro.voice_for("en"), kokoro.voice_for("it")) == ("am_liam", "im_nicola")


def test_both_engines_are_registered_with_qwen3_constructed_without_loading() -> None:
    assert set(TTS_ENGINES) == {"qwen3", "kokoro"}
    assert TTS_ENGINES["qwen3"]().voice_for("it") == "jarvis-it"
