"""The User's language, English or Italian, and the voice Jarvis answers in."""

import re
from functools import cache
from typing import Literal

from lingua import Language, LanguageDetector, LanguageDetectorBuilder

Lang = Literal["en", "it"]

# American English only: never a British voice (bf_*, bm_*).
VOICES: dict[Lang, str] = {"en": "am_liam", "it": "im_nicola"}

# A single word needs more confidence: "Hello" scores 0.69 Italian.
CONFIDENT = 0.8
CONFIDENT_ONE_WORD = 0.9

_CODES: dict[Language, Lang] = {Language.ENGLISH: "en", Language.ITALIAN: "it"}

# Function words of one language and not the other ("a", "in", "me", "come" are both), counted first: lingua alone
# reads "Apri una issue su pumpkit per il bug del login" as English, from its English nouns.
_FUNCTION_WORDS: dict[Lang, frozenset[str]] = {
    "en": frozenset(
        "the an is are was were of for to what which on and my you your it this that these please with how "
        "has have do does can could would should i'm it's what's there be at from all any still why who "
        "when where not just".split()
    ),
    "it": frozenset(
        "il lo la gli le un una uno di del della dei delle degli su sul sulla sui per che è sono cosa non con "
        "mi ti ci quali quale questo questa perché ho hai ha se al alla allo nel nella nei da dal dalla ma "
        "anche sì grazie ancora tutto tutti fammi dimmi quando chi".split()
    ),
}
# An elided article or preposition before a vowel ("l'issue", "c'è"), never English's "c'mon"; or "po'".
_ELISION = re.compile(r"^(?:(?:l|dell|all|nell|dall|sull|un|c|d|quest)'[aeiouhàèéìòù]|po'$)", re.IGNORECASE)
_WORDS = re.compile(r"[\w']+")


@cache
def _detector() -> LanguageDetector:
    return LanguageDetectorBuilder.from_languages(*_CODES).with_preloaded_language_models().build()


def warm_up() -> None:
    """Loads the language models, about 40 ms, so the first turn does not pay for it."""
    _detector()


def detect_language(text: str, previous: Lang) -> Lang:
    """The language of `text`, or `previous` when detection is not confident."""
    found = _WORDS.findall(text)
    # Acronyms such as "CI" and "IT" are not the Italian words they spell; "I" is English past the first word,
    # where Italian's article "i" would be lower case.
    words = [w.lower() for w in found if not (len(w) > 1 and w.isupper())]
    en = sum(w in _FUNCTION_WORDS["en"] for w in words) + found[1:].count("I")
    it = sum(w in _FUNCTION_WORDS["it"] or bool(_ELISION.match(w)) for w in words)
    if en != it:
        return "en" if en > it else "it"
    values = _detector().compute_language_confidence_values(text)
    if not values:
        return previous
    best = values[0]
    needed = CONFIDENT_ONE_WORD if len(text.split()) < 2 else CONFIDENT
    return _CODES[best.language] if best.value >= needed else previous


def voice_for(lang: Lang) -> str:
    return VOICES[lang]
