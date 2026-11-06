"""Source-bound cumulative observations; decisions grant no Task or Approval authority."""

from dataclasses import dataclass, field
from typing import Protocol

from jarvis_hub.voice.ownership import SpokenTurn
from jarvis_hub.voice.language import Lang


@dataclass(frozen=True)
class Source:
    candidate: int
    revision: int
    text: str
    started_at: float
    speech_at: float
    observed_at: float
    final: bool = False
    pauses: tuple[tuple[float, float], ...] = ()
    lang: Lang | None = None

    @property
    def key(self) -> tuple[int, int]:
        return self.candidate, self.revision


@dataclass(frozen=True)
class Snapshot:
    id: str
    sources: tuple[Source, ...]
    at: float
    phase: str
    preceding_request: str
    played_text: str
    request: tuple[str, int] | None
    played_basis: str = 'queue_estimate'
    pauses: tuple[tuple[float, float], ...] = ()
    active_sources: tuple[int, ...] = ()


@dataclass(frozen=True)
class Decision:
    snapshot_id: str
    directed: tuple[tuple[int, int], ...]
    floor: bool
    ready: bool
    voice_only: bool
    reason: str
    unrelated: tuple[tuple[int, int], ...] = ()


class CumulativeRecognition(Protocol):
    async def assess_snapshot(self, snapshot: Snapshot) -> Decision: ...


@dataclass(eq=False)
class TalkingPeriod:
    epoch: int
    owner: SpokenTurn | None
    phase: str
    preceding_request: str
    played_text: str
    opened: float
    sources: dict[int, Source] = field(default_factory=lambda: dict[int, Source]())
    active: set[int] = field(default_factory=lambda: set[int]())
    version: int = 0
    sequence: int = 0
    stopped: bool = False
    dirty: bool = False
    reassess_at: float = float('inf')
    pauses: list[tuple[float, float]] = field(default_factory=lambda: list[tuple[float, float]]())
    last_speech_at: float | None = None
