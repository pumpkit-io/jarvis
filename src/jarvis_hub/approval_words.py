"""How the Hub reads words for an Approval: a yes from a closed lexicon, the words that hold it back, the action verbs
and the numbers said, in English and Italian. The lexicons are data files beside this module, read once."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

LEXICON_DIR = Path(__file__).parent / "approval_lexicons"

Words = tuple[str, ...]

_WORDS = re.compile(r"[\w']+")


def words(text: str) -> Words:
    """Lowercase words, with hyphens and punctuation gone and curly apostrophes made straight."""
    return tuple(_WORDS.findall(text.lower().replace("’", "'")))


def _lines(path: Path) -> list[Words]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [w for line in lines if not line.lstrip().startswith("#") and (w := words(line))]


def found(said: Sequence[str], phrase: Sequence[str]) -> list[int]:
    """Where `phrase` starts in `said`."""
    n = len(phrase)
    return [i for i in range(len(said) - n + 1) if tuple(said[i : i + n]) == tuple(phrase)] if n else []


@dataclass(frozen=True)
class Lexicons:
    yes: frozenset[Words]
    hold: frozenset[Words]
    # Each verb's kind, such as "merge" for "unisci".
    verbs: dict[str, str]

    @staticmethod
    def load(folder: Path) -> "Lexicons":
        verbs: dict[str, str] = {}
        for line in (folder / "verbs.txt").read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#") or ":" not in line:
                continue
            kind, said = line.split(":", 1)
            verbs |= {verb: kind.strip() for verb in words(said)}
        return Lexicons(frozenset(_lines(folder / "yes.txt")), frozenset(_lines(folder / "hold.txt")), verbs)

    def holding_back(self, text: str) -> str | None:
        """What in `text` holds a yes back: a question mark, a word ending in n't, or a phrase of the hold lexicon."""
        if "?" in text:
            return "a question"
        said = words(text)
        for word in said:
            if word.endswith("n't"):
                return f'"{word}"'
        for phrase in sorted(self.hold, key=lambda p: -len(p)):
            if found(said, phrase):
                return f'"{" ".join(phrase)}"'
        return None

    def says_yes(self, said: Words) -> bool:
        return any(found(said, phrase) for phrase in self.yes)

    def kinds(self, said: Iterable[str]) -> frozenset[str]:
        """The kinds of action the verbs in `said` name."""
        return frozenset(self.verbs[w] for w in said if w in self.verbs)


LEXICONS = Lexicons.load(LEXICON_DIR)


def _english(n: int) -> list[Words]:
    units = (
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen"
    ).split()
    tens = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
    if n < 20:
        return [(units[n],)]
    if n < 100:
        return [(tens[n // 10],) + ((units[n % 10],) if n % 10 else ())]
    hundreds = [(units[n // 100], "hundred")] + ([("a", "hundred")] if n // 100 == 1 else [])
    if n % 100 == 0:
        return hundreds
    return [h + a + r for h in hundreds for a in ((), ("and",)) for r in _english(n % 100)]


def _italian(n: int) -> list[Words]:
    units = (
        "_ uno due tre quattro cinque sei sette otto nove dieci undici dodici tredici quattordici quindici sedici "
        "diciassette diciotto diciannove"
    ).split()
    tens = "_ _ venti trenta quaranta cinquanta sessanta settanta ottanta novanta".split()
    if n < 20:
        return [(units[n],)]
    if n < 100:
        ten, unit = tens[n // 10], n % 10
        if not unit:
            return [(ten,)]
        # Ventuno, ventotto: the tens drop their vowel before uno and otto; ventitré takes an accent when joined.
        joined = (ten[:-1] if unit in (1, 8) else ten) + ("tré" if unit == 3 else units[unit])
        return [(joined,), (joined.replace("tré", "tre"),), (ten, units[unit])]
    hundred = "cento" if n // 100 == 1 else units[n // 100] + "cento"
    if n % 100 == 0:
        return [(hundred,)]
    rest = _italian(n % 100)
    # Centottanta, as well as centoottanta.
    elided = [(hundred[:-1] + r[0],) for r in rest if len(r) == 1 and r[0].startswith("o")]
    return [(hundred + r[0],) for r in rest if len(r) == 1] + elided + [(hundred,) + r for r in rest]


# "one", "uno" and "a" alone are not numbers: "yes, that one" names nothing.
_NUMBER_WORDS: dict[Words, int] = {
    form: n
    for n in range(1, 1000)
    for form in _english(n) + _italian(n)
    if form not in (("one",), ("uno",))
}
_LONGEST = max(len(form) for form in _NUMBER_WORDS)


def numbers(said: Words) -> frozenset[int]:
    """The whole numbers said, in digits or as English or Italian words up to 999, longest reading first."""
    out: set[int] = set()
    i = 0
    while i < len(said):
        if said[i].isdigit():
            out.add(int(said[i]))
            i += 1
            continue
        for n in range(min(_LONGEST, len(said) - i), 0, -1):
            if (value := _NUMBER_WORDS.get(tuple(said[i : i + n]))) is not None:
                out.add(value)
                i += n
                break
        else:
            i += 1
    return frozenset(out)
