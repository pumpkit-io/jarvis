"""Adapters the tests inject into the Hub core: a scripted agent backend, a manual clock, recorders."""

import asyncio
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from jarvis_hub.hub import WARM_UP
from jarvis_hub.agents.backend import (
    AgentEvent,
    SessionSpec,
    TextDelta,
    TextEnd,
    ToolCall,
    TurnEnd,
    Usage,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@dataclass(frozen=True)
class Wait:
    seconds: float


@dataclass(frozen=True)
class Text:
    """One delta; consecutive Texts form one block, closed by a Tool, Fail or the end of the turn."""

    text: str


@dataclass(frozen=True)
class Tool:
    name: str
    input: dict[str, Any] = field(default_factory=lambda: {})


@dataclass(frozen=True)
class AwaitInterrupt:
    """Blocks the turn until the Hub interrupts it; the steps after it are the late deltas the SDK still sends."""


@dataclass(frozen=True)
class Fail:
    error: str


Step = Wait | Text | Tool | AwaitInterrupt | Fail | Usage


class FakeSession:
    def __init__(self, backend: "FakeBackend", spec: SessionSpec) -> None:
        self.backend = backend
        self.spec = spec
        self.received: list[str] = []
        self.tool_results: list[str] = []
        self.closed = False
        self._interrupted = asyncio.Event()

    async def send(self, message: str) -> AsyncIterator[AgentEvent]:
        self.received.append(message)
        self._interrupted.clear()
        script = self.backend.next_script(message)
        usage, in_text = Usage(), False
        for step in script:
            await asyncio.sleep(0)
            if isinstance(step, Text):
                in_text = True
                yield TextDelta(step.text)
                continue
            if in_text and isinstance(step, (Tool, Fail)):
                in_text = False
                yield TextEnd()
            match step:
                case Wait(seconds):
                    self.backend.clock.advance(seconds)
                case Tool(name, args):
                    yield ToolCall(name, args)
                    handler = next((t.handler for t in self.spec.tools if t.name == name), None)
                    if handler is not None:
                        self.tool_results.append(await handler(args))
                case AwaitInterrupt():
                    await self._interrupted.wait()
                case Fail(error):
                    yield TurnEnd("failed", usage, error)
                    return
                case Usage():
                    usage = step
        if in_text:
            yield TextEnd()
        yield TurnEnd("interrupted" if self._interrupted.is_set() else "completed", usage)

    async def interrupt(self) -> None:
        self._interrupted.set()

    async def close(self) -> None:
        self.closed = True


class FakeBackend:
    """Plays one script per message sent, in order; `warm_up` answers the Hub's warm-up message."""

    def __init__(
        self,
        clock: FakeClock,
        scripts: Iterable[Sequence[Step]] = (),
        warm_up: Sequence[Step] = (Text("Ready."),),
    ) -> None:
        self.clock = clock
        self.scripts = list(scripts)
        self.warm_up = warm_up
        self.sessions: list[FakeSession] = []

    async def open(self, spec: SessionSpec) -> FakeSession:
        session = FakeSession(self, spec)
        self.sessions.append(session)
        return session

    def next_script(self, message: str) -> Sequence[Step]:
        if message == WARM_UP:
            return self.warm_up
        return self.scripts.pop(0) if self.scripts else (Text("Very good, sir."),)


class RecordingOutput:
    def __init__(self) -> None:
        self.log: list[tuple[str, str]] = []
        self.on_text: asyncio.Event = asyncio.Event()

    def reply(self, text: str) -> None:
        self.log.append(("reply", text))
        self.on_text.set()

    def reply_end(self) -> None:
        self.log.append(("end", ""))

    def notice(self, text: str) -> None:
        self.log.append(("notice", text))

    @property
    def replies(self) -> list[str]:
        out: list[str] = []
        current = ""
        for kind, text in self.log:
            if kind == "reply":
                current += text
            elif kind == "end":
                out.append(current)
                current = ""
        return out

    @property
    def notices(self) -> list[str]:
        return [text for kind, text in self.log if kind == "notice"]


class RecordingMetrics:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, entry: dict[str, Any]) -> None:
        self.records.append(entry)


async def lines(*texts: str) -> AsyncIterator[str]:
    for text in texts:
        yield text
