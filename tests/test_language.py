import pytest

from jarvis_hub.voice.language import Lang, detect_language, voice_for


@pytest.mark.parametrize(
    ("text", "previous", "expected"),
    [
        ("What are you working on?", "it", "en"),
        ("Open a pull request for the login fix", "it", "en"),
        ("thanks", "it", "en"),
        ("Su cosa stai lavorando?", "en", "it"),
        ("Apri una issue su pumpkit per il bug del login", "en", "it"),
        ("grazie", "en", "it"),
        ("sì", "en", "it"),
        ("Chiudi i ticket vecchi", "en", "it"),
        ("CI is green", "it", "en"),
        ("C'mon, run it", "it", "en"),
        ("Ok. I test sono verdi", "en", "it"),
        ("Ok. I think the tests are green", "it", "en"),
        ("Yes, I can do that", "it", "en"),
        ("C'è una PR aperta", "en", "it"),
        ("Fammi un po'", "en", "it"),
        # Too short or mixed to tell: the previous language stays.
        ("ok", "it", "it"),
        ("ok", "en", "en"),
        ("Hello", "en", "en"),
        ("PR 42", "it", "it"),
        ("Merge pull request 42.", "it", "it"),
        ("42", "it", "it"),
        ("", "it", "it"),
    ],
)
def test_detects_english_or_italian_and_keeps_the_previous_language_when_unsure(
    text: str, previous: Lang, expected: Lang
) -> None:
    assert detect_language(text, previous) == expected


def test_each_language_has_its_voice_and_none_is_british() -> None:
    assert voice_for("en") == "am_liam"
    assert voice_for("it") == "im_nicola"
