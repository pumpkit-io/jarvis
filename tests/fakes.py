"""Adapters the tests inject into the Hub core: a scripted agent backend, a manual clock, recorders."""

import asyncio
import threading
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from jarvis_hub.agents.backend import (
    AgentEvent,
    SessionSpec,
    TextDelta,
    TextEnd,
    ToolCall,
    TurnEnd,
    Usage,
)
from jarvis_hub.hub import WARM_UP, Hub, Line
from jarvis_hub.voice.player import Clip, Samples


class FakeClock:
    """Time moves only when a test or a script's Wait advances it; sleepers wake when it passes their deadline."""

    def __init__(self) -> None:
        self.t = 1000.0
        self._sleepers: list[asyncio.Event] = []

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds
        for sleeper in self._sleepers:
            sleeper.set()
        self._sleepers.clear()

    async def sleep(self, seconds: float) -> None:
        deadline = self.t + seconds
        while self.t < deadline:
            woken = asyncio.Event()
            self._sleepers.append(woken)
            await woken.wait()


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


@dataclass(frozen=True)
class Gate:
    """Blocks the turn until the test sets the event."""

    event: asyncio.Event


@dataclass(frozen=True)
class Set:
    """Sets the event, releasing whatever a Gate holds elsewhere."""

    event: asyncio.Event


@dataclass(frozen=True)
class Raise:
    """The session itself raises, as a broken transport would."""

    error: Exception


@dataclass(frozen=True)
class Abort:
    """Ends the turn as interrupted although nobody called interrupt(), as when the CLI aborts on its own."""


Step = Wait | Text | Tool | AwaitInterrupt | Gate | Set | Fail | Raise | Abort | Usage


@dataclass(frozen=True)
class ToolResult:
    name: str
    input: dict[str, Any]
    result: str | None


class FakeSession:
    """Calls the Hub's tool handlers as the CLI would, after reporting the call."""

    def __init__(self, backend: "FakeBackend", spec: SessionSpec) -> None:
        self.backend = backend
        self.spec = spec
        self.received: list[str] = []
        self.tool_results: list[ToolResult] = []
        self.closed = False
        self._sending = False
        self._interrupted = asyncio.Event()

    async def send(self, message: str) -> AsyncIterator[AgentEvent]:
        self.received.append(message)
        self._interrupted.clear()
        self._sending = True
        try:
            async for event in self._play(self.backend.next_script(self.spec.role, message)):
                yield event
        finally:
            self._sending = False

    async def _play(self, script: Sequence[Step]) -> AsyncIterator[AgentEvent]:
        usage, in_text = Usage(), False
        for step in script:
            await asyncio.sleep(0)
            if isinstance(step, Text):
                in_text = True
                yield TextDelta(step.text)
                continue
            if in_text and isinstance(step, (Tool, Fail, Abort)):
                in_text = False
                yield TextEnd()
            match step:
                case Wait(seconds):
                    self.backend.clock.advance(seconds)
                case Tool(name, args):
                    yield ToolCall(name, args)
                    handler = next((t.handler for t in self.spec.tools if t.name == name), None)
                    result = await handler(args) if handler else None
                    self.tool_results.append(ToolResult(name, args, result))
                case AwaitInterrupt():
                    await self._interrupted.wait()
                case Gate(event):
                    await event.wait()
                case Set(event):
                    event.set()
                case Raise(error):
                    raise error
                case Fail(error):
                    yield TurnEnd("failed", usage, error)
                    return
                case Abort():
                    yield TurnEnd("interrupted", usage)
                    return
                case Usage():
                    usage = step
        if in_text:
            yield TextEnd()
        yield TurnEnd("interrupted" if self._interrupted.is_set() else "completed", usage)

    async def interrupt(self) -> None:
        # Like the CLI: the interrupt lands on whatever turn runs when it arrives, or on none.
        if self.backend.interrupt_gate is not None:
            await self.backend.interrupt_gate.wait()
        if self.backend.interrupt_error is not None:
            raise self.backend.interrupt_error
        if self._sending:
            self._interrupted.set()

    async def close(self) -> None:
        self.closed = True


class FakeBackend:
    """Plays one script per message, in order, per role; `warm_up` answers the Talker's warm-up message.

    An Orchestrator with no script left ends its turn without a report.
    """

    def __init__(
        self,
        clock: FakeClock,
        scripts: Iterable[Sequence[Step]] = (),
        warm_up: Sequence[Step] = (Text("Ready."),),
        open_error: Exception | None = None,
        orchestrator: Iterable[Sequence[Step]] = (),
        orchestrator_open_error: Exception | None = None,
        interrupt_gate: asyncio.Event | None = None,
        interrupt_error: Exception | None = None,
    ) -> None:
        self.clock = clock
        self.scripts = list(scripts)
        self.orchestrator_scripts = list(orchestrator)
        self.warm_up = warm_up
        self.open_error = open_error
        self.orchestrator_open_error = orchestrator_open_error
        self.interrupt_gate = interrupt_gate
        self.interrupt_error = interrupt_error
        self.sessions: list[FakeSession] = []

    async def open(self, spec: SessionSpec) -> FakeSession:
        error = self.orchestrator_open_error if spec.role == "orchestrator" else self.open_error
        if error is not None:
            raise error
        session = FakeSession(self, spec)
        self.sessions.append(session)
        return session

    def session(self, role: str) -> FakeSession:
        return next(s for s in self.sessions if s.spec.role == role)

    def all_sessions(self, role: str) -> list[FakeSession]:
        return [s for s in self.sessions if s.spec.role == role]

    def next_script(self, role: str, message: str) -> Sequence[Step]:
        if role == "orchestrator":
            return self.orchestrator_scripts.pop(0) if self.orchestrator_scripts else ()
        if message == WARM_UP:
            return self.warm_up
        return self.scripts.pop(0) if self.scripts else (Text("Very good, sir."),)


class RecordingOutput:
    def __init__(self) -> None:
        self.log: list[tuple[str, str]] = []
        self.on_text: asyncio.Event = asyncio.Event()

    def heard(self, text: str) -> None:
        self.log.append(("heard", text))

    def reply(self, text: str) -> None:
        self.log.append(("reply", text))
        self.on_text.set()

    def reply_end(self) -> None:
        self.log.append(("end", ""))

    def notice(self, text: str) -> None:
        self.log.append(("notice", text))

    def show(self, markdown: str) -> None:
        self.log.append(("show", markdown))

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

    @property
    def shown(self) -> list[str]:
        return [text for kind, text in self.log if kind == "show"]


class RecordingMetrics:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, entry: dict[str, Any]) -> None:
        self.records.append(entry)


async def lines(clock: FakeClock, *texts: str) -> AsyncIterator[Line]:
    for text in texts:
        yield Line(text, clock.now())


TALKER = SessionSpec(role="talker", model="sonnet", effort="low", thinking=False, system_prompt="You are Jarvis.", cwd=Path("/tmp"))
ORCHESTRATOR = SessionSpec(
    role="orchestrator", model="sonnet", effort="medium", thinking=True, system_prompt="You orchestrate.", cwd=Path("/tmp")
)


def make_hub(backend: FakeBackend, output: RecordingOutput, metrics: Any, **options: Any) -> Hub:
    return Hub(
        talker=TALKER,
        orchestrator=ORCHESTRATOR,
        backend=backend,
        clock=backend.clock,
        output=output,
        metrics=metrics,
        **options,
    )


class Typist:
    """Lines typed one at a time while the Hub runs; `end()` closes the input."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()

    def type(self, text: str) -> None:
        self._queue.put_nowait(text)

    def end(self) -> None:
        self._queue.put_nowait(None)

    async def lines(self) -> AsyncIterator[Line]:
        while (text := await self._queue.get()) is not None:
            yield Line(text, self._clock.now())


async def until(condition: Any, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.001)


class FakeTts:
    """Instant synthesis: a twentieth of a second per character, every sample 0.1."""

    sample_rate = 24_000

    def __init__(self, load_error: Exception | None = None, fail_on: str | None = None) -> None:
        self.load_error = load_error
        self.fail_on = fail_on
        self.calls: list[tuple[str, str]] = []
        # Set by a test to hold synthesis until it is cleared; the Hub runs synthesis off the event loop.
        self.hold = threading.Event()
        self.hold.set()

    def load(self) -> None:
        if self.load_error is not None:
            raise self.load_error

    def synthesize(self, text: str, voice: str) -> Samples:
        self.hold.wait(timeout=5)
        self.calls.append((text, voice))
        if text == self.fail_on:
            raise RuntimeError("synthesis failed")
        return np.full(len(text) * self.sample_rate // 20, 0.1, dtype=np.float32)


class RecordingPlayer:
    """Reports what was played. Clips play at once, or with `hold` stay queued until `finish()`."""

    def __init__(self, hold: bool = False) -> None:
        self.hold = hold
        self.clips: list[Clip] = []
        self.events: list[str] = []
        self._queued: list[Clip] = []
        self._heard: list[str] = []
        self.closed = False

    def play(self, clip: Clip, *, starts_reply: bool = False) -> None:
        self.clips.append(clip)
        self.events.append(f"play {clip.text}")
        if starts_reply:
            self._heard = []
        if self.hold:
            self._queued.append(clip)
        else:
            self._heard.append(clip.text)

    def finish(self) -> None:
        self._heard += [c.text for c in self._queued]
        self._queued.clear()

    def pause(self) -> None:
        self.events.append("pause")

    def resume(self, rewind_ms: int = 300) -> None:
        self.events.append(f"resume {rewind_ms}")

    def stop(self) -> None:
        self.events.append("stop")
        self._queued.clear()

    def played(self) -> str:
        return " ".join(self._heard)

    def pending(self) -> float:
        return sum(c.seconds for c in self._queued)

    def close(self) -> None:
        self.closed = True

    @property
    def texts(self) -> list[str]:
        return [c.text for c in self.clips]

    @property
    def voices(self) -> list[str]:
        return [c.voice for c in self.clips]
