"""Identity and bounded verbatim revisions of an admitted spoken turn."""

from dataclasses import dataclass

from jarvis_hub.voice.language import Lang

MAX_REQUEST_CHARACTERS = 6000


@dataclass(frozen=True)
class SpokenTurn:
    id: str
    revision: int
    text: str
    arrived: float
    lang: Lang | None
    segments: tuple[str, ...] = ()
    semantic_start: int = 0

    def __post_init__(self) -> None:
        if not self.segments:
            object.__setattr__(self, "segments", (self.text,))

    @property
    def semantic_text(self) -> str:
        return " ".join(self.segments[self.semantic_start:]).strip()

    @property
    def token(self) -> tuple[str, int]:
        return self.id, self.revision

    def amended(self, text: str, arrived: float, lang: Lang | None, *, segments: tuple[str, ...] = ()) -> 'SpokenTurn':
        combined = f'{self.text} {text}'.strip()
        if len(combined) > MAX_REQUEST_CHARACTERS:
            raise ValueError('Spoken request reached its retention limit; split or repeat the request.')
        return SpokenTurn(self.id, self.revision + 1, combined, arrived, lang or self.lang,
                          self.segments + (segments or (text,)), self.semantic_start)
