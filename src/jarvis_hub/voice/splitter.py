"""Cuts the Talker's streamed text into the pieces text-to-speech says one at a time."""

import re

# Closing quotes, brackets and markdown emphasis may follow the mark: "**Done.** Next".
_SENTENCE_END = re.compile(r"[.?!…]+[\"'”’)\]*_]*(?=\s)")
_CLAUSE_END = re.compile(r"[,;:](?=\s)")
_LAST_WORD = re.compile(r"(\S+)$")
# Such as "a.m.", "U.S.": a sentence end only when the next word starts with a capital.
_DOTTED = re.compile(r"(?:[a-z]\.)+[a-z]")

# Compared without the final dot and in lower case; a single letter counts too, as an initial.
# Left out: words that also end English sentences, such as "on." and "art."
ABBREVIATIONS = frozenset(
    {
        # English
        "mr", "mrs", "ms", "dr", "prof", "st", "jr", "sr", "vs", "etc", "e.g", "i.e", "approx", "inc", "ltd",
        "fig", "vol", "dept",
        # Italian
        "sig", "sig.ra", "sigg", "dott", "dott.ssa", "ing", "avv", "geom", "ecc", "pag", "pagg",
        "nr", "tel", "cfr", "es",
    }
)

# Words a first clause needs before it goes out on its own.
CLAUSE_WORDS = 6


class SentenceSplitter:
    """One per reply. A sentence ends at `.?!` and a space, or at a block's end; the first clause, at `,;:` and a space
    once it has six words. Numbers, abbreviations, URLs and code are never cut, and a piece with no word is dropped."""

    def __init__(self) -> None:
        self._buffer = ""
        self._emitted = 0

    def feed(self, text: str) -> list[str]:
        self._buffer += text
        out: list[str] = []
        while (cut := self._next_cut()) is not None:
            out += self._take(cut)
        return out

    def end_block(self) -> list[str]:
        """The rest of the text, at the end of a text block or of the reply.

        Text either side of a tool call arrives with no space between, so a block's end is a sentence end.
        """
        return self._take(len(self._buffer))

    def _take(self, cut: int) -> list[str]:
        chunk, self._buffer = self._buffer[:cut].strip(), self._buffer[cut:]
        if not any(c.isalnum() for c in chunk):
            return []
        self._emitted += 1
        return [chunk]

    def _next_cut(self) -> int | None:
        cuts = [m.end() for m in _SENTENCE_END.finditer(self._buffer) if self._ends_sentence(m.start(), m.end())]
        if self._emitted == 0:
            cuts += [m.end() for m in _CLAUSE_END.finditer(self._buffer) if self._ends_first_clause(m.end())]
        return min(cuts, default=None)

    def _ends_sentence(self, start: int, end: int) -> bool:
        if self._in_code(end):
            return False
        before = self._buffer[:start]
        if re.fullmatch(r"\s*\d+", before):
            return False  # A list number such as "1." is not a sentence.
        if self._buffer[start] != ".":
            return True
        word = _LAST_WORD.search(before)
        if word is None:
            return True
        word_text = word.group(1).lstrip("(\"'“‘*_").lower()
        if word_text in ABBREVIATIONS or (len(word_text) == 1 and word_text.isalpha()):
            return False
        if _DOTTED.fullmatch(word_text):
            following = self._buffer[end:].lstrip()
            return following[:1].isupper()
        return True

    def _ends_first_clause(self, end: int) -> bool:
        return not self._in_code(end) and len(self._buffer[:end].split()) >= CLAUSE_WORDS

    def _in_code(self, at: int) -> bool:
        return self._buffer[:at].count("`") % 2 == 1
