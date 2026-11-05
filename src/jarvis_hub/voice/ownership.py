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

    @property
    def token(self) -> tuple[str, int]:
        return self.id, self.revision

    def amended(self, text: str, arrived: float, lang: Lang | None) -> 'SpokenTurn':
        combined = f'{self.text} {text}'.strip()
        if len(combined) > MAX_REQUEST_CHARACTERS:
            raise ValueError('Spoken request reached its retention limit; split or repeat the request.')
        return SpokenTurn(self.id, self.revision + 1, combined, arrived, lang or self.lang)
