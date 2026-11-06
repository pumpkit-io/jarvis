"""Source-time eligibility and bounded speech retained by one completed reply's window."""

from dataclasses import dataclass, field

from jarvis_hub.voice.state import Heard
from jarvis_hub.voice.ownership import SpokenTurn


@dataclass(eq=False)
class FollowUpWindow:
    token: str
    epoch: int
    opened: float
    expires: float
    admitted_turn: str
    played_text: str
    source: SpokenTurn | None
    retained: tuple[Heard, ...] = ()
    waiting: list[Heard] = field(default_factory=lambda: list[Heard]())
    inflight: tuple[Heard, ...] = ()
    active: set[int | None] = field(default_factory=lambda: set[int | None]())
    onsets: dict[int | None, float] = field(default_factory=lambda: dict[int | None, float]())
    version: int = 0

    def includes(self, onset: float) -> bool:
        return self.opened <= onset <= self.expires
