from collections.abc import Iterable
from pathlib import Path

import numpy as np
import pytest

from jarvis_hub.voice.language import Lang
from jarvis_hub.voice.player import Samples
from jarvis_hub.config import VoiceSettings
from jarvis_hub.voice.tts import (
    TTS_ENGINES,
    ClonedVoice,
    KokoroSynthesizer,
    Qwen3Synthesizer,
    make_synthesizer,
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


def pack(root: Path, name: str, language: Lang, transcript: str, trim_db: float) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "reference.wav").write_bytes(b"RIFF")
    (root / name / "voice.toml").write_text(
        f'language = "{language}"\ntranscript = "{transcript}"\ntrim_db = {trim_db}\n', encoding="utf-8")


def voices(tmp_path: Path) -> dict[Lang, str]:
    pack(tmp_path, "clone-en", "en", "An English transcript.", -2.0)
    pack(tmp_path, "clone-it", "it", "Una trascrizione italiana.", -4.3)
    return {"en": "clone-en", "it": "clone-it"}


def loaded(tmp_path: Path, model: FakeCloner) -> Qwen3Synthesizer:
    synth = Qwen3Synthesizer(voices(tmp_path), tmp_path, lambda: model)
    synth.load()
    return synth


def test_each_language_speaks_its_own_clip_transcript_and_language(tmp_path: Path) -> None:
    model = FakeCloner()
    synth = loaded(tmp_path, model)

    list(synth.stream("Good evening, sir.", synth.voice_for("en")))
    list(synth.stream("Buonasera, signore.", synth.voice_for("it")))

    assert [(text, v.clip.name, v.transcript, v.language) for text, v in model.calls] == [
        ("Good evening, sir.", "reference.wav", "An English transcript.", "English"),
        ("Buonasera, signore.", "reference.wav", "Una trascrizione italiana.", "Italian"),
    ]
    assert [v.clip.parent.name for _, v in model.calls] == ["clone-en", "clone-it"]


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


def test_a_broken_voice_fails_load_naming_it_and_the_fallback(tmp_path: Path) -> None:
    chosen = voices(tmp_path)
    (tmp_path / "clone-it" / "reference.wav").unlink()
    synth = Qwen3Synthesizer(chosen, tmp_path, lambda: FakeCloner())

    with pytest.raises(ValueError, match=r"clone-it.*reference\.wav.*JARVIS_TTS_ENGINE=kokoro"):
        synth.load()


def test_a_voice_chosen_for_the_wrong_language_is_refused_at_load(tmp_path: Path) -> None:
    voices(tmp_path)
    synth = Qwen3Synthesizer({"en": "clone-it", "it": "clone-it"}, tmp_path, lambda: FakeCloner())

    with pytest.raises(ValueError, match=r"JARVIS_VOICE_EN.*clone-it.*Italian"):
        synth.load()


def test_an_unknown_voice_name_is_refused_at_load_naming_the_setting(tmp_path: Path) -> None:
    voices(tmp_path)
    synth = Qwen3Synthesizer({"en": "nobody", "it": "clone-it"}, tmp_path, lambda: FakeCloner())

    with pytest.raises(ValueError, match=r"JARVIS_VOICE_EN.*no voice named 'nobody'"):
        synth.load()


def test_an_unknown_voice_is_refused(tmp_path: Path) -> None:
    synth = loaded(tmp_path, FakeCloner())

    with pytest.raises(ValueError, match="am_liam"):
        list(synth.stream("Hello.", "am_liam"))


def test_kokoro_speaks_american_english_and_italian_never_a_british_voice() -> None:
    kokoro = KokoroSynthesizer()
    assert (kokoro.voice_for("en"), kokoro.voice_for("it")) == ("am_liam", "im_nicola")


def test_both_engines_are_registered_with_qwen3_constructed_without_loading() -> None:
    assert set(TTS_ENGINES) == {"qwen3", "kokoro"}
    assert TTS_ENGINES["qwen3"](VoiceSettings()).voice_for("it") == "jarvis-it"


def test_the_configured_voices_and_the_users_folder_reach_the_qwen3_synthesizer(tmp_path: Path) -> None:
    voices(tmp_path)
    settings = VoiceSettings(voice_en="clone-en", voice_it="clone-it", user_voices=tmp_path)

    synth = make_synthesizer(settings)

    assert (synth.voice_for("en"), synth.voice_for("it")) == ("clone-en", "clone-it")
    assert isinstance(synth, Qwen3Synthesizer) and synth.user_voices == tmp_path
