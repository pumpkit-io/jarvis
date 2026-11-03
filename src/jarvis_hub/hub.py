"""The Hub core: turns User input into Talker turns, hands Tasks to the Orchestrator, and relays its reports.

Every outside dependency is injected (agent backend, clock, output, metrics) so tests and text mode share
this one code path; voice mode will add audio adapters around it.
"""

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, replace
from html import escape
from typing import Any, Literal, Protocol

from jarvis_hub.agents.backend import (
    AgentBackend,
    AgentEvent,
    AgentSession,
    HubTool,
    SessionSpec,
    TextDelta,
    TextEnd,
    ToolCall,
    TurnEnd,
)
from jarvis_hub.tasks import FINISHED, Task, Tasks


class Clock(Protocol):
    def now(self) -> float:
        """Monotonic seconds."""
        ...


class Output(Protocol):
    def heard(self, text: str) -> None:
        """The User's line, as the Talker starts on it."""
        ...

    def reply(self, text: str) -> None:
        """A piece of the Talker's reply, shown as soon as it arrives."""
        ...

    def reply_end(self) -> None: ...

    def notice(self, text: str) -> None:
        """A line from the Hub itself, never from the Talker."""
        ...

    def show(self, markdown: str) -> None:
        """Details for the screen, never spoken."""
        ...


class Metrics(Protocol):
    def record(self, entry: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class Line:
    """A line the User typed, stamped when it was read rather than when the Hub got to it."""

    text: str
    arrived: float


# Sent once at launch, in the background, so the first real turn finds the prompt cached (ADR 0006); never shown.
WARM_UP = '<event source="hub">Session start. Reply with one word.</event>'

# The Phase 0 definition, kept so Hub numbers compare with the spike's: the end of a delta counts too.
_SENTENCE_END = re.compile(r"[.?!](\s|$)")

TalkerKind = Literal["warm_up", "user", "event"]


def user_message(text: str) -> str:
    # Escaped so typed text can never close the tag or forge another one.
    return f'<user typed="true">{escape(text, quote=False)}</user>'


def event_message(source: str, text: str, task: str, *, needs_user: bool = False, details: bool = False) -> str:
    on_screen = ' details="on screen"' if details else ""
    flag = "true" if needs_user else "false"
    return f'<event source="{source}" task="{task}" needs_user="{flag}"{on_screen}>{escape(text, quote=False)}</event>'


def task_message(task: Task, request: str, context: str, *, continues: bool) -> str:
    opening = f'<task id="{task.id}" continues="true">' if continues else f'<task id="{task.id}">'
    parts = [opening, f"<request>{escape(request, quote=False)}</request>"]
    if context:
        parts.append(f"<context>{escape(context, quote=False)}</context>")
    return "\n".join([*parts, "</task>"])


@dataclass(frozen=True)
class _TalkerInput:
    message: str
    kind: TalkerKind
    arrived: float
    details: str | None = None
    heard: str | None = None


@dataclass(frozen=True)
class _OrchestratorInput:
    task_id: str
    message: str
    arrived: float


def _schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required)}


_STRING = {"type": "string"}


class Hub:
    def __init__(
        self,
        *,
        talker: SessionSpec,
        orchestrator: SessionSpec,
        backend: AgentBackend,
        clock: Clock,
        output: Output,
        metrics: Metrics,
    ) -> None:
        self._talker_spec = replace(talker, tools=self._talker_tools())
        self._orchestrator_spec = replace(orchestrator, tools=self._orchestrator_tools())
        self._backend = backend
        self._clock = clock
        self._output = output
        self._metrics = metrics
        self._tasks = Tasks()
        self._talker: AgentSession | None = None
        self._orchestrator: AgentSession | None = None
        self._talker_inbox: asyncio.Queue[_TalkerInput] = asyncio.Queue()
        # One queue, so the Orchestrator's single session takes one message at a time.
        self._orchestrator_inbox: asyncio.Queue[_OrchestratorInput] = asyncio.Queue()
        self._loops: list[asyncio.Task[None]] = []
        self._pending = 0
        self._settled = asyncio.Event()
        self._settled.set()
        self._turn_count = 0
        self._reply_turn: int | None = None
        self._interrupted_turn: int | None = None
        # Held while an interrupt is on its way, so it cannot land on the turn after the one it was meant for.
        self._interrupt_lock = asyncio.Lock()
        self._orchestrating: str | None = None

    async def start(self) -> None:
        """Open the Talker and warm it in the background (ADR 0006); a line typed meanwhile waits for it."""
        self._loops = [asyncio.create_task(self._talk()), asyncio.create_task(self._orchestrate())]

    async def run(self, inputs: AsyncIterator[Line]) -> None:
        """Turns from typed lines until the input ends and every Task has gone quiet, or the Talker cannot start."""
        talking = self._loops[0]
        reading = asyncio.create_task(self._read(inputs))
        await asyncio.wait([reading, talking], return_when=asyncio.FIRST_COMPLETED)
        if not reading.done():
            reading.cancel()
            await asyncio.wait([reading])
            return
        await reading
        settling = asyncio.create_task(self._settled.wait())
        await asyncio.wait([settling, talking], return_when=asyncio.FIRST_COMPLETED)
        settling.cancel()

    async def interrupt(self) -> bool:
        """Stop the reply in progress, if any. Returns whether there was one."""
        turn = self._reply_turn
        if turn is None or self._talker is None:
            return False
        self._interrupted_turn = turn
        async with self._interrupt_lock:
            await self._talker.interrupt()
        return True

    async def close(self) -> None:
        for loop in self._loops:
            loop.cancel()
        if self._loops:
            await asyncio.wait(self._loops)
        for loop in self._loops:
            if not loop.cancelled() and (error := loop.exception()):
                self._output.notice(f"The Hub stopped: {error!r}")
        for session in (self._talker, self._orchestrator):
            if session is not None:
                await session.close()
        self._talker = self._orchestrator = None

    async def _read(self, inputs: AsyncIterator[Line]) -> None:
        async for line in inputs:
            if text := line.text.strip():
                self._post_talker(_TalkerInput(user_message(text), "user", line.arrived, heard=text))

    def _post_talker(self, item: _TalkerInput) -> None:
        self._pending += 1
        self._settled.clear()
        self._talker_inbox.put_nowait(item)

    def _post_orchestrator(self, item: _OrchestratorInput) -> None:
        self._pending += 1
        self._settled.clear()
        self._orchestrator_inbox.put_nowait(item)

    def _handled(self) -> None:
        self._pending -= 1
        if self._pending == 0:
            self._settled.set()

    async def _talk(self) -> None:
        try:
            self._talker = await self._backend.open(self._talker_spec)
            await self._turn(WARM_UP, kind="warm_up", arrived=self._clock.now())
        except Exception as e:
            self._output.notice(f"The Talker could not start: {e}")
            return
        while True:
            item = await self._talker_inbox.get()
            try:
                async with self._interrupt_lock:
                    pass
                if item.heard:
                    self._output.heard(item.heard)
                if item.details:
                    self._output.show(item.details)
                await self._turn(item.message, kind=item.kind, arrived=item.arrived)
            finally:
                self._handled()

    async def _turn(self, message: str, *, kind: TalkerKind, arrived: float) -> None:
        assert self._talker is not None
        self._turn_count += 1
        turn = self._turn_count
        show = kind != "warm_up"
        t0 = self._clock.now()
        shown, block = "", ""
        first_delta: float | None = None
        first_sentence: float | None = None
        tools: list[str] = []
        tool_before_text = False
        after_block = False
        end = TurnEnd("failed", error="the session ended without a result")
        self._reply_turn = turn if show else None
        try:
            async for event in self._guarded(self._talker.send(message)):
                match event:
                    case TextDelta(text):
                        if self._interrupted_turn == turn:
                            continue
                        if first_delta is None:
                            first_delta = self._clock.now()
                        if after_block and shown and not shown[-1].isspace() and not text[:1].isspace():
                            text = " " + text
                        after_block = False
                        shown += text
                        block += text
                        if first_sentence is None and _SENTENCE_END.search(block):
                            first_sentence = self._clock.now()
                        if show:
                            self._output.reply(text)
                    case TextEnd():
                        if first_sentence is None and block.strip():
                            first_sentence = self._clock.now()
                        block, after_block = "", True
                    case ToolCall(name):
                        if first_delta is None and not tools:
                            tool_before_text = True
                        tools.append(name)
                    case TurnEnd():
                        end = event
        finally:
            self._reply_turn = None
        if end.outcome == "interrupted" and self._interrupted_turn != turn:
            end = TurnEnd("failed", end.usage, "the turn stopped without the Hub interrupting it")
        if show:
            self._output.reply_end()
        if end.outcome == "failed":
            self._output.notice(f"The Talker failed: {end.error}")

        def ms(t: float | None) -> int | None:
            return None if t is None else round((t - arrived) * 1000)

        self._record(
            {
                "kind": kind,
                "role": self._talker_spec.role,
                "model": self._talker_spec.model,
                "effort": self._talker_spec.effort,
                "wait_ms": ms(t0),
                "first_delta_ms": ms(first_delta),
                "first_sentence_ms": ms(first_sentence),
                "total_ms": ms(self._clock.now()),
                "tools": tools,
                "tool_before_text": tool_before_text,
                "outcome": end.outcome,
                "usage": asdict(end.usage),
            }
        )

    async def _orchestrate(self) -> None:
        while True:
            item = await self._orchestrator_inbox.get()
            try:
                await self._orchestrator_turn(item)
            finally:
                self._handled()

    async def _orchestrator_turn(self, item: _OrchestratorInput) -> None:
        task = self._tasks.get(item.task_id)
        if task is None or task.cancelled:
            return
        if self._orchestrator is None:
            try:
                self._orchestrator = await self._backend.open(self._orchestrator_spec)
            except Exception as e:
                self._fail(task, f"the Orchestrator could not start: {e}")
                return
        task.state = "working"
        reports = task.reports
        t0 = self._clock.now()
        tools: list[str] = []
        end = TurnEnd("failed", error="the session ended without a result")
        self._orchestrating = task.id
        try:
            async for event in self._guarded(self._orchestrator.send(item.message)):
                match event:
                    case ToolCall(name):
                        tools.append(name)
                    case TurnEnd():
                        end = event
                    case _:
                        pass
        finally:
            self._orchestrating = None
        if not task.cancelled and task.reports == reports:
            if end.outcome == "completed":
                self._fail(task, "the Orchestrator ended its turn without a report")
            else:
                self._fail(task, end.error or "the Orchestrator's turn was stopped")
        self._record(
            {
                "kind": "task",
                "role": self._orchestrator_spec.role,
                "model": self._orchestrator_spec.model,
                "effort": self._orchestrator_spec.effort,
                "task": task.id,
                "wait_ms": round((t0 - item.arrived) * 1000),
                "total_ms": round((self._clock.now() - item.arrived) * 1000),
                "tools": tools,
                "outcome": end.outcome,
                "usage": asdict(end.usage),
            }
        )

    async def _guarded(self, events: AsyncIterator[AgentEvent]) -> AsyncIterator[AgentEvent]:
        """A session that raises ends its turn as failed, so one bad turn never stops the Hub."""
        try:
            async for event in events:
                yield event
        except Exception as e:
            yield TurnEnd("failed", error=f"{type(e).__name__}: {e}")

    def _fail(self, task: Task, reason: str) -> None:
        task.state = "failed"
        task.last_report = f"Failed: {reason}."
        self._post_talker(
            _TalkerInput(event_message("hub", f"{task.id} failed: {reason}.", task.id), "event", self._clock.now())
        )

    def _record(self, entry: dict[str, Any]) -> None:
        try:
            self._metrics.record(entry)
        except Exception as e:
            self._output.notice(f"Could not write metrics: {e}")

    def _talker_tools(self) -> tuple[HubTool, ...]:
        return (
            HubTool(
                "delegate",
                "Hand work that needs the Org to the Orchestrator as a Task. Returns at once with the Task id, and "
                "the result arrives later as an event, so there is nothing to add after calling it. "
                "Pass task_id to continue a Task, such as answering its question.",
                _schema(
                    {
                        "request": {**_STRING, "description": "What the User wants, in their words."},
                        "context": {**_STRING, "description": "Anything from the conversation the request depends on."},
                        "task_id": {**_STRING, "description": "The Task this continues, such as T3."},
                    },
                    ("request",),
                ),
                self._delegate,
            ),
            HubTool(
                "status",
                "List this session's Tasks: state, age, request and last report. Answered at once.",
                _schema({}),
                self._status,
            ),
            HubTool(
                "cancel",
                "Cancel a Task by id.",
                _schema({"task_id": {**_STRING, "description": "Such as T3."}}, ("task_id",)),
                self._cancel,
            ),
            HubTool(
                "show",
                "Put markdown on the User's screen, never spoken: lists, tables, plans, links, code.",
                _schema({"markdown": _STRING}, ("markdown",)),
                self._show,
            ),
            HubTool(
                "end_conversation",
                "The User is done for now: stop listening after this reply.",
                _schema({}),
                self._end_conversation,
            ),
        )

    def _orchestrator_tools(self) -> tuple[HubTool, ...]:
        return (
            HubTool(
                "report",
                "Tell the User about a Task. The only way to reach them.",
                _schema(
                    {
                        "task_id": _STRING,
                        "spoken": {**_STRING, "description": "One sentence, written to be heard."},
                        "details_markdown": {**_STRING, "description": "Details for the screen."},
                        "needs_user": {"type": "boolean", "description": "True when the Task waits on the User."},
                    },
                    ("task_id", "spoken", "needs_user"),
                ),
                self._report,
            ),
        )

    async def _delegate(self, args: dict[str, Any]) -> str:
        request = str(args.get("request") or "").strip()
        context = str(args.get("context") or "").strip()
        if not request:
            return "request is required."
        if args.get("task_id"):
            task = self._tasks.get(args["task_id"])
            if task is None:
                return f"There is no Task {args['task_id']}. Delegate without task_id to start a new one."
            if task.cancelled:
                return f"{task.id} was cancelled. Delegate without task_id to start a new one."
            task.state = "queued"
            continues = True
        else:
            task = self._tasks.create(request, self._clock.now())
            continues = False
        message = task_message(task, request, context, continues=continues)
        self._post_orchestrator(_OrchestratorInput(task.id, message, self._clock.now()))
        return json.dumps({"task_id": task.id, "queued": True})

    async def _status(self, args: dict[str, Any]) -> str:
        return self._tasks.status(self._clock.now())

    async def _cancel(self, args: dict[str, Any]) -> str:
        task = self._tasks.get(args.get("task_id"))
        if task is None:
            return f"There is no Task {args.get('task_id')}."
        if task.state in FINISHED:
            return f"{task.id} is already {task.state}."
        task.state = "cancelled"
        if self._orchestrating == task.id and self._orchestrator is not None:
            await self._orchestrator.interrupt()
        return f"Cancelled {task.id}."

    async def _show(self, args: dict[str, Any]) -> str:
        markdown = str(args.get("markdown") or "")
        if not markdown.strip():
            return "markdown is required."
        self._output.show(markdown)
        return "Shown on screen."

    async def _end_conversation(self, args: dict[str, Any]) -> str:
        # Text mode has no listening to stop, so the Hub only acknowledges it.
        return "The conversation ends after this reply."

    async def _report(self, args: dict[str, Any]) -> str:
        task = self._tasks.get(args.get("task_id"))
        if task is None:
            return f"There is no Task {args.get('task_id')}."
        if task.cancelled:
            return f"{task.id} was cancelled; the User will not hear this."
        spoken = str(args.get("spoken") or "").strip()
        if not spoken:
            return "spoken is required."
        details = str(args.get("details_markdown") or "").strip() or None
        needs_user = args.get("needs_user") in (True, "true")
        task.state = "needs_user" if needs_user else "done"
        task.last_report = spoken
        task.reports += 1
        message = event_message("orchestrator", spoken, task.id, needs_user=needs_user, details=details is not None)
        self._post_talker(_TalkerInput(message, "event", self._clock.now(), details))
        return "Reported to the User."
