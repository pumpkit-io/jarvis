"""Adapters the tests inject into the Hub core: a scripted agent backend, a manual clock, recorders."""

import asyncio
import threading
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import CoroutineType
from typing import Any, Literal

import numpy as np

from jarvis_hub.agents.backend import (
    AgentEvent,
    Context,
    SessionSpec,
    TextDelta,
    TextEnd,
    ToolCall,
    TurnEnd,
    Usage,
)
from jarvis_hub.hub import WARM_UP, Hub, Line
from jarvis_hub.limits import UsageLimit
from jarvis_hub.voice.language import Lang
from jarvis_hub.subagents import Refusal, Subagent, SubagentSetup
from jarvis_hub.waits import WaitSetup
from jarvis_hub.voice.player import Clip, Samples
from jarvis_hub.voice.state import Heard, SpeechStarted, WakeWord


class FakeClock:
    """Time moves only when a test or a script's Wait advances it; sleepers wake when it passes their deadline."""

    def __init__(self) -> None:
        self.t = 1000.0
        self._sleepers: list[asyncio.Event] = []

    def now(self) -> float:
        return self.t

    @property
    def sleepers(self) -> int:
        return len(self._sleepers)

    def advance(self, seconds: float) -> None:
        self.t += seconds
        for sleeper in self._sleepers:
            sleeper.set()
        self._sleepers.clear()

    def sleep(self, seconds: float) -> "CoroutineType[Any, Any, None]":
        # The deadline is taken at the call: a task made to sleep may first run after the test advanced the clock.
        return self._sleep_until(self.t + seconds)

    async def _sleep_until(self, deadline: float) -> None:
        while self.t < deadline:
            woken = asyncio.Event()
            self._sleepers.append(woken)
            try:
                await woken.wait()
            finally:
                if woken in self._sleepers:
                    self._sleepers.remove(woken)


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


@dataclass(frozen=True)
class Limited:
    """The usage limit ends the turn, resetting `seconds` from now (None: the event gave no time)."""

    seconds: float | None = None


@dataclass(frozen=True)
class NoContext:
    """The turn ends without a context, for this reason."""

    reason: str


Step = Wait | Text | Tool | AwaitInterrupt | Gate | Set | Fail | Raise | Abort | Limited | Usage | Context | NoContext


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
        usage, context, in_text = Usage(), None, False
        no_context: str | None = None
        for step in script:
            await asyncio.sleep(0)
            if isinstance(step, Text):
                in_text = True
                yield TextDelta(step.text)
                continue
            if in_text and isinstance(step, (Tool, Fail, Abort, Limited)):
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
                    yield TurnEnd("failed", usage, error, context)
                    return
                case Abort():
                    yield TurnEnd("interrupted", usage, context=context)
                    return
                case Limited(seconds):
                    resets_at = None if seconds is None else self.backend.wall() + seconds
                    yield TurnEnd("limited", usage, "the usage limit was reached", context, resets_at=resets_at)
                    return
                case Usage():
                    usage = step
                case Context():
                    context = step
                case NoContext(reason):
                    no_context = reason
        if in_text:
            yield TextEnd()
        outcome: Literal["completed", "interrupted"] = "interrupted" if self._interrupted.is_set() else "completed"
        yield TurnEnd(outcome, usage, context=context, no_context=no_context)

    async def interrupt(self) -> None:
        # Like the CLI: the interrupt lands on whatever turn runs when it arrives, or on none.
        if self.backend.interrupt_gate is not None:
            await self.backend.interrupt_gate.wait()
        if self.backend.interrupt_error is not None:
            raise self.backend.interrupt_error
        if self._sending:
            self._interrupted.set()

    async def close(self) -> None:
        if self.spec.role == "subagent" and self.backend.subagent_close_gate is not None:
            await self.backend.subagent_close_gate.wait()
        self.closed = True


class FakeBackend:
    """Plays one script per message, in order, per role; `warm_up` answers the Talker's warm-up message and
    `seed_reply` the seed a restarted Talker starts with.

    An Orchestrator with no script left ends its turn without a report.
    """

    def __init__(
        self,
        clock: FakeClock,
        scripts: Iterable[Sequence[Step]] = (),
        warm_up: Sequence[Step] = (Text("Ready."),),
        seed_reply: Sequence[Step] = (Text("Ready."),),
        restart_error: Exception | None = None,
        restart_open_gate: asyncio.Event | None = None,
        open_error: Exception | None = None,
        orchestrator: Iterable[Sequence[Step]] = (),
        orchestrator_open_error: Exception | None = None,
        interrupt_gate: asyncio.Event | None = None,
        interrupt_error: Exception | None = None,
        subagents: dict[str, Sequence[Step]] | None = None,
        subagent_open_error: Exception | None = None,
        subagent_open_gate: asyncio.Event | None = None,
        subagent_close_gate: asyncio.Event | None = None,
    ) -> None:
        self.clock = clock
        self.scripts = list(scripts)
        self.orchestrator_scripts = list(orchestrator)
        self.warm_up = warm_up
        self.seed_reply = seed_reply
        self.restart_error = restart_error
        # Holds a restarted Talker's open until set: a CLI slow to start.
        self.restart_open_gate = restart_open_gate
        self.talker_opens = 0
        self.open_error = open_error
        self.orchestrator_open_error = orchestrator_open_error
        self.interrupt_gate = interrupt_gate
        self.interrupt_error = interrupt_error
        self.sessions: list[FakeSession] = []
        # A Subagent's script, by its brief.
        self.subagent_scripts = dict(subagents or {})
        self.subagent_open_error = subagent_open_error
        # Holds a Subagent's open, or its session's close, until set: a CLI that never answers.
        self.subagent_open_gate = subagent_open_gate
        self.subagent_close_gate = subagent_close_gate

    def wall(self) -> float:
        """Unix seconds for the Hub's usage limit: the fake clock, so a reset comes when a test advances it."""
        return self.clock.now()

    async def open(self, spec: SessionSpec) -> FakeSession:
        if spec.role == "subagent":
            if self.subagent_open_gate is not None:
                await self.subagent_open_gate.wait()
            if self.subagent_open_error is not None:
                raise self.subagent_open_error
        error = self.orchestrator_open_error if spec.role == "orchestrator" else self.open_error
        if spec.role == "talker":
            self.talker_opens += 1
            if self.all_sessions("talker"):
                error = self.restart_error
                if self.restart_open_gate is not None:
                    await self.restart_open_gate.wait()
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
        if role == "subagent":
            return self.subagent_scripts.get(message, (Text("Done."),))
        if message == WARM_UP:
            return self.warm_up
        if message.startswith("<event source=\"hub\" reason=\"restart\""):
            return self.seed_reply
        return self.scripts.pop(0) if self.scripts else (Text("Very good, sir."),)


def idle_limit(clock: FakeClock) -> UsageLimit:
    """A usage limit that nothing has hit, for a Subagents built without a Hub."""
    return UsageLimit(clock, clock.now, lambda role, opened: None, lambda roles, everyone: None)


class RecordingOutput:
    def __init__(self) -> None:
        self.log: list[tuple[str, str]] = []
        self.on_text: asyncio.Event = asyncio.Event()

    def heard(self, text: str, *, typed: bool = True) -> None:
        self.log.append(("heard" if typed else "said", text))

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


class FakeWorkspace:
    """Org repos are the names in `org`; a missing one is cloned. Worktrees are paths under /wt, never made."""

    def __init__(self, org: Iterable[str] = ("pumpkit", "jarvis", "pumpkit-ops"), cloned: Iterable[str] = ()) -> None:
        self.org = set(org)
        self.cloned = set(cloned)
        self.clones: list[str] = []
        self.worktrees: list[tuple[str, str]] = []
        self.locks: dict[Path, str] = {}
        self.released: list[Path] = []
        # What describe() says is on each worktree's branch.
        self.on_branch: dict[Path, str] = {}
        self.scratches: list[Path] = []
        self.discarded: list[Path] = []
        # Why discard() keeps a scratch worktree, by path; one not listed is removed.
        self.keep: dict[Path, str] = {}
        # When set, describe() waits on it, as a hung git would.
        self.describe_gate: asyncio.Event | None = None

    async def repo(self, name: str) -> tuple[str, Path]:
        # A real workspace awaits git here, so another spawn may run meanwhile.
        await asyncio.sleep(0)
        short = name.removeprefix("pumpkit-io/")
        if short not in self.org:
            raise Refusal(f"{name} is outside the Org pumpkit-io.")
        if short not in self.cloned:
            self.cloned.add(short)
            self.clones.append(short)
        return short, Path("/org") / short

    async def worktree(self, clone: Path, branch: str, holder: str) -> tuple[Path, str]:
        path = Path("/wt") / clone.name / branch.split("/")[-1]
        self.worktrees.append((clone.name, branch))
        self.locks[path] = holder
        return path, branch

    async def scratch(self, clone: Path, name: str, holder: str) -> Path:
        path = Path("/wt") / clone.name / name
        self.scratches.append(path)
        self.locks[path] = holder
        return path

    async def release(self, worktree: Path, holder: str) -> str | None:
        if self.locks.get(worktree, holder) != holder:
            return f"its lock is held by {self.locks[worktree]} now, so it was left in place."
        self.released.append(worktree)
        self.locks.pop(worktree, None)
        return None

    async def discard(self, worktree: Path, holder: str) -> str | None:
        if self.locks.get(worktree, holder) != holder:
            return f"its lock is held by {self.locks[worktree]} now, so it was left in place."
        self.locks.pop(worktree, None)
        self.discarded.append(worktree)
        return self.keep.get(worktree)

    async def describe(self, worktree: Path) -> str:
        if self.describe_gate is not None:
            await self.describe_gate.wait()
        return self.on_branch.get(worktree, "No commits beyond origin/main.")


def subagent_spec(subagent: Subagent) -> SessionSpec:
    return SessionSpec(
        role="subagent",
        model=subagent.model,
        effort=subagent.effort,
        thinking=True,
        system_prompt="You do the work.",
        cwd=subagent.cwd,
        skip_permissions=True,
    )


def make_hub(
    backend: FakeBackend,
    output: RecordingOutput,
    metrics: Any,
    workspace: FakeWorkspace | None = None,
    log_dir: Path | None = None,
    max_effort: Any = "max",
    cap: int = 3,
    time_limit: float = 3600.0,
    runner: "StubRunner | None" = None,
    watch_limit: float = 6 * 3600.0,
    **options: Any,
) -> Hub:
    options.setdefault("wall", backend.wall)
    setup = SubagentSetup(
        session=subagent_spec,
        workspace=workspace or FakeWorkspace(),
        log_dir=log_dir,
        max_effort=max_effort,
        cap=cap,
        time_limit=time_limit,
    )
    return Hub(
        talker=TALKER,
        orchestrator=ORCHESTRATOR,
        backend=backend,
        clock=backend.clock,
        output=output,
        metrics=metrics,
        subagents=setup,
        waits=WaitSetup(org="pumpkit-io", runner=runner or StubRunner(), watch_limit=watch_limit),
        **options,
    )


class StubRunner:
    """Stands in for a Watcher's gh processes: a `view` answers `view_result` at once; a watch blocks until the test
    ends it with `finish`."""

    def __init__(self, view_result: tuple[int, str] = (0, "{}")) -> None:
        self.view_result = view_result
        self.viewed: list[tuple[str, ...]] = []
        self.started: list[tuple[str, ...]] = []
        self.killed: list[tuple[str, ...]] = []
        self._ends: dict[tuple[str, ...], asyncio.Future[tuple[int, str]]] = {}

    async def __call__(self, argv: tuple[str, ...]) -> tuple[int, str]:
        if argv[2] == "view":
            self.viewed.append(argv)
            return self.view_result
        self.started.append(argv)
        end = self._ends[argv] = asyncio.get_running_loop().create_future()
        try:
            return await end
        except asyncio.CancelledError:
            self.killed.append(argv)
            raise

    def finish(self, index: int, code: int, output: str) -> None:
        self._ends[self.started[index]].set_result((code, output))


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


class ScriptedAudio:
    """Audio input as the pipeline reports it: Wake word hits, speech onsets and transcribed turns, sent by the test."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self._queue: asyncio.Queue[WakeWord | SpeechStarted | Heard] = asyncio.Queue()
        self.capturing: list[bool] = []

    def wake(self) -> None:
        self._queue.put_nowait(WakeWord(self._clock.now()))

    def speech(self) -> None:
        self._queue.put_nowait(SpeechStarted(self._clock.now()))

    def say(self, text: str, lang: Lang | None = None) -> None:
        """Speech onset, then the turn's transcript at its end."""
        self.speech()
        self._queue.put_nowait(Heard(text, lang, self._clock.now()))

    def capture(self, on: bool) -> None:
        self.capturing.append(on)

    async def events(self) -> AsyncIterator[WakeWord | SpeechStarted | Heard]:
        while True:
            yield await self._queue.get()
