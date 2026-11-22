import time

import pytest

from jarvis_hub.voice.language import Lang, detect_language


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
        ("Ok. I test passano", "it", "it"),
        ("Ok. I test passano", "en", "it"),
        ("Yes, I did it per spec", "it", "en"),
        ("Ok. I ticket sono chiusi", "en", "it"),
        ("I think so", "it", "en"),
        ("Right. I will check", "it", "en"),
        ("Sì. I bug sono risolti", "en", "it"),
        ("Perfetto. I am ready", "it", "en"),
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


def test_a_very_long_text_is_detected_from_its_start_in_a_moment() -> None:
    # A long reply is detected on the Hub's event loop at the end of each turn.
    started = time.perf_counter()

    assert detect_language("Buongiorno signor Stark. " + "Pull request aperta. " * 20000, "en") == "it"
    assert time.perf_counter() - started < 1
