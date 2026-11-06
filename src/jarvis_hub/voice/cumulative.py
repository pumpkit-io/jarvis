"""Source-bound cumulative observations; decisions grant no Task or Approval authority."""

from dataclasses import dataclass, field, replace
import re
import string
from typing import Protocol

from jarvis_hub.voice.ownership import SpokenTurn
from jarvis_hub.voice.language import Lang

MAX_PERIOD_SOURCES = 32
MAX_PERIOD_SECONDS = 120.0
MAX_ASSESSMENTS = 320
MAX_CONTEXT_CHARACTERS = 24_000
MAX_WORD_QUESTIONS = 512


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
    allowed_from: int = 0

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
class Selection:
    candidate: int
    revision: int
    start: int
    end: int

    @property
    def key(self) -> tuple[int, int]:
        return self.candidate, self.revision


def word_spans(source: Source, *, include_retired: bool = False) -> tuple[Selection, ...]:
    return tuple(Selection(source.candidate, source.revision, match.start(), match.end())
                 for match in re.finditer(r"\S+", source.text) if include_retired or match.start() >= source.allowed_from)


def retired_prefix(source: Source) -> tuple[str, ...]:
    return tuple(source.text[word.start:word.end].casefold().strip(string.punctuation + '…“”‘’')
                 for word in word_spans(source, include_retired=True))


def after_retired_prefix(source: Source, prefix: tuple[str, ...]) -> Source | None:
    words = word_spans(source, include_retired=True)
    if retired_prefix(source)[:len(prefix)] != prefix:
        return None
    boundary = words[len(prefix)].start if len(words) > len(prefix) else len(source.text)
    return replace(source, allowed_from=boundary)


@dataclass(frozen=True)
class Decision:
    snapshot_id: str
    directed: tuple[tuple[int, int], ...]
    floor: bool
    ready: bool
    voice_only: bool
    reason: str
    unrelated: tuple[tuple[int, int], ...] = ()
    spans: tuple[Selection, ...] = ()


def selected_sources(snapshot: Snapshot, decision: Decision) -> tuple[Source, ...]:
    keys = {source.key for source in snapshot.sources}
    directed, unrelated = set(decision.directed), set(decision.unrelated)
    if (not directed.issubset(keys) or not unrelated.issubset(keys) or directed & unrelated
            or len(directed) != len(decision.directed) or len(decision.spans) > MAX_WORD_QUESTIONS):
        raise ValueError('invalid_source_selection')
    if any(span.key not in keys or span.key in directed | unrelated for span in decision.spans):
        raise ValueError('invalid_source_selection')
    selected: list[Source] = []
    for source in snapshot.sources:
        if source.key in directed:
            if source.text[source.allowed_from:].strip():
                selected.append(replace(source, text=source.text[source.allowed_from:], allowed_from=0))
            continue
        spans = sorted((span for span in decision.spans if span.key == source.key), key=lambda span: span.start)
        words = word_spans(source)
        starts, ends = {word.start for word in words}, {word.end for word in words}
        previous = -1
        for span in spans:
            if span.start not in starts or span.end not in ends or span.start >= span.end or span.start < previous:
                raise ValueError('invalid_source_span')
            selected.append(replace(source, text=source.text[span.start:span.end]))
            previous = span.end
    return tuple(selected)


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
    admitted: SpokenTurn | None = None
    dirty: bool = False
    reassess_at: float = float('inf')
    pauses: list[tuple[float, float]] = field(default_factory=lambda: list[tuple[float, float]]())
    last_speech_at: float | None = None
    failed_at: float | None = None
    retired_prefixes: dict[int, tuple[str, ...]] = field(default_factory=lambda: dict[int, tuple[str, ...]]())
    source_revisions: dict[int, int] = field(default_factory=lambda: dict[int, int]())

    @property
    def expires_at(self) -> float:
        return min(self.opened + MAX_PERIOD_SECONDS, self.failed_at + 30 if self.failed_at is not None else float('inf'))
