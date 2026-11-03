"""The Hub core: turns User input into Talker turns, hands Tasks to the Orchestrator, and relays its reports.

Every outside dependency is injected (agent backend, clock, output, metrics, speech) so tests and text mode
share this one code path.
"""

import asyncio
import contextlib
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
    Refused,
    SessionSpec,
    TextDelta,
    TextEnd,
    ToolCall,
    TurnEnd,
)
from jarvis_hub.orchestrator import orchestrator_tools
from jarvis_hub.subagents import Subagent, SubagentSetup, Subagents
from jarvis_hub.talker import talker_tools
from jarvis_hub.tasks import Task, Tasks, no_task
from jarvis_hub.voice.language import Lang, detect_language


class Clock(Protocol):
    def now(self) -> float:
        """Monotonic seconds."""
        ...

    async def sleep(self, seconds: float) -> None: ...


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


class Speech(Protocol):
    """Says the Talker's replies aloud as they stream (`jarvis_hub.voice.speech.Speech`)."""

    def begin(self, lang: Lang, *, turn: int, heard_at: float | None) -> None: ...

    def feed(self, text: str) -> None: ...

    def block_end(self) -> None: ...

    def end(self) -> None: ...

    def stop(self) -> None: ...


class _Silent:
    def begin(self, lang: Lang, *, turn: int, heard_at: float | None) -> None:
        pass

    def feed(self, text: str) -> None:
        pass

    def block_end(self) -> None:
        pass

    def end(self) -> None:
        pass

    def stop(self) -> None:
        pass


@dataclass(frozen=True)
class Line:
    """A line the User typed, stamped when it was read rather than when the Hub got to it."""

    text: str
    arrived: float
    # Left out, the Hub detects it from the text.
    lang: Lang | None = None


# Sent once at launch, in the background, so the first real turn finds the prompt cached (ADR 0006); never shown.
WARM_UP = '<event source="hub">Session start. Reply with one word.</event>'

# The Phase 0 definition, kept so Hub numbers compare with the spike's: the end of a delta counts too.
_SENTENCE_END = re.compile(r"[.?!](\s|$)")

TalkerKind = Literal["warm_up", "user", "event"]

# An Orchestrator turn is a look-up of a few commands; one running this long is stuck.
ORCHESTRATOR_TIMEOUT = 600.0
# How long an interrupted turn has to end before the Hub gives up on its session.
INTERRUPT_GRACE = 10.0


def user_message(text: str, lang: Lang) -> str:
    # Escaped so typed text can never close the tag or forge another one.
    return f'<user lang="{lang}" typed="true">{escape(text, quote=False)}</user>'


def event_message(source: str, text: str, task: str, *, needs_user: bool = False, details: bool = False) -> str:
    on_screen = ' details="on screen"' if details else ""
    flag = "true" if needs_user else "false"
    return f'<event source="{source}" task="{task}" needs_user="{flag}"{on_screen}>{escape(text, quote=False)}</event>'


def subagent_message(subagent: Subagent, status: str, text: str) -> str:
    return (
        f'<event source="subagent" id="{subagent.id}" task="{subagent.task_id}" status="{status}">'
        f"{escape(text, quote=False)}</event>"
    )


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
    lang: Lang | None = None


@dataclass(frozen=True)
class _OrchestratorInput:
    task_id: str
    message: str
    arrived: float


class _TurnGate:
    """Lets an interrupt land only on the turn it was meant for, never on the one after."""

    def __init__(self) -> None:
        self.turn: int | None = None
        self._lock = asyncio.Lock()

    async def interrupt(self, session: AgentSession, turn: int) -> bool:
        if self.turn != turn:
            return False
        async with self._lock:
            await session.interrupt()
        return True

    async def before_next_turn(self) -> None:
        # Taking the lock waits out an interrupt still on its way to a turn that has ended.
        async with self._lock:
            pass


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
        subagents: SubagentSetup,
        orchestrator_timeout: float = ORCHESTRATOR_TIMEOUT,
        speech: Speech | None = None,
    ) -> None:
        tools = talker_tools(
            delegate=self._delegate,
            status=self._status,
            cancel=self._cancel,
            show=self._show,
            end_conversation=self._end_conversation,
        )
        self._talker_spec = replace(talker, tools=tools)
        self._orchestrator_spec = replace(
            orchestrator,
            tools=orchestrator_tools(
                report=self._report,
                spawn_subagent=self._spawn_subagent,
                list_subagents=self._list_subagents,
                cancel_subagent=self._cancel_subagent,
            ),
        )
        self._backend = backend
        self._clock = clock
        self._output = output
        self._metrics = metrics
        self._orchestrator_timeout = orchestrator_timeout
        self._speech: Speech = speech if speech is not None else _Silent()
        # The language of the latest line read, and of the latest User turn the Talker took: replies follow it.
        self._read_lang: Lang = "en"
        self._lang: Lang = "en"
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
        self._talker_gate = _TurnGate()
        self._interrupted_turn: int | None = None
        self._orchestrator_gate = _TurnGate()
        self._orchestrating: str | None = None
        self._subagents = Subagents(
            setup=subagents,
            backend=backend,
            clock=clock,
            on_end=self._subagent_ended,
            on_over_time=self._subagent_over_time,
        )

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
        """Stop the reply in progress and whatever is still being said. Returns whether a Talker turn was running."""
        self._speech.stop()
        turn = self._talker_gate.turn
        if turn is None or self._talker is None:
            return False
        self._interrupted_turn = turn
        return await self._talker_gate.interrupt(self._talker, turn)

    async def close(self) -> None:
        await self._subagents.close()
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
                self._read_lang = lang = line.lang or detect_language(text, self._read_lang)
                message = user_message(text, lang)
                self._post(self._talker_inbox, _TalkerInput(message, "user", line.arrived, heard=text, lang=lang))

    def _post(self, inbox: "asyncio.Queue[Any]", item: _TalkerInput | _OrchestratorInput) -> None:
        """Queue an item; the Hub counts as settled again once every posted item is handled."""
        self._pending += 1
        self._settled.clear()
        inbox.put_nowait(item)

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
                await self._talker_gate.before_next_turn()
                if item.lang:
                    self._lang = item.lang
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
        self._talker_gate.turn = turn if show else None
        speech = self._speech if show else _Silent()
        speech.begin(self._lang, turn=turn, heard_at=arrived if kind == "user" else None)
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
                        speech.feed(text)
                    case TextEnd():
                        speech.block_end()
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
            self._talker_gate.turn = None
            if self._interrupted_turn != turn:
                speech.end()
        if end.outcome == "interrupted" and self._interrupted_turn != turn:
            end = TurnEnd("failed", end.usage, "the turn stopped without the Hub interrupting it")
        if show:
            self._output.reply_end()
        if end.outcome == "failed":
            self._output.notice(f"The Talker failed: {end.error}")

        def ms(t: float | None) -> int | None:
            return None if t is None else round((t - arrived) * 1000)

        self._record(
            self._talker_spec,
            kind,
            end,
            tools,
            wait_ms=ms(t0),
            first_delta_ms=ms(first_delta),
            first_sentence_ms=ms(first_sentence),
            total_ms=ms(self._clock.now()),
            tool_before_text=tool_before_text,
        )

    async def _orchestrate(self) -> None:
        while True:
            item = await self._orchestrator_inbox.get()
            try:
                await self._orchestrator_gate.before_next_turn()
                await self._orchestrator_turn(item)
            except Exception as e:
                # Whatever went wrong on this Task, the next one in the queue still gets its turn.
                error = f"{type(e).__name__}: {e}"
                task = self._tasks.get(item.task_id)
                if task is not None and task.state == "working":
                    self._fail(task, error)
                self._output.notice(f"The Orchestrator failed on {item.task_id}: {error}")
            finally:
                self._handled()

    async def _orchestrator_turn(self, item: _OrchestratorInput) -> None:
        task = self._tasks.get(item.task_id)
        if task is None:
            return
        if self._orchestrator is None:
            try:
                self._orchestrator = await self._backend.open(self._orchestrator_spec)
            except Exception as e:
                if task.take_message():
                    self._fail(task, f"the Orchestrator could not start: {e}")
                return
        # Taken after the last await before the turn, so a cancel lands either before it or on the running turn.
        if not task.take_message():
            return
        session = self._orchestrator
        self._turn_count += 1
        turn = self._turn_count
        reports = task.reports
        t0 = self._clock.now()
        tools: list[str] = []
        self._orchestrating = task.id
        consuming = asyncio.create_task(self._consume(task, session, item.message, turn, tools))
        try:
            end = await self._within_time(consuming, session, turn)
        finally:
            self._orchestrating = None
            self._orchestrator_gate.turn = None
            if not consuming.done():
                # A turn left mid-stream would feed its leftovers to the next message, so the session goes.
                consuming.cancel()
                await asyncio.wait([consuming])
                self._orchestrator = None
                with contextlib.suppress(Exception):
                    await session.close()
        # A turn that started Subagents may end without a report: their completion events bring the next turn.
        if not task.cancelled and task.reports == reports and not task.subagents:
            if end.outcome == "completed":
                self._fail(task, "the Orchestrator ended its turn without a report")
            else:
                self._fail(task, end.error or "the Orchestrator's turn was stopped")
        self._record(
            self._orchestrator_spec,
            "task",
            end,
            tools,
            task=task.id,
            wait_ms=round((t0 - item.arrived) * 1000),
            total_ms=round((self._clock.now() - item.arrived) * 1000),
        )

    async def _consume(self, task: Task, session: AgentSession, message: str, turn: int, tools: list[str]) -> TurnEnd:
        if task.cancelled:
            return TurnEnd("interrupted")
        # Interruptible only from here, as the message goes out: an interrupt reaching the session earlier is lost.
        self._orchestrator_gate.turn = turn
        end = TurnEnd("failed", error="the session ended without a result")
        async for event in self._guarded(session.send(message)):
            match event:
                case ToolCall(name):
                    tools.append(name)
                case TurnEnd():
                    end = event
                case TextDelta() | TextEnd():
                    pass
        return end

    async def _within_time(self, consuming: "asyncio.Task[TurnEnd]", session: AgentSession, turn: int) -> TurnEnd:
        """The turn's end, or a failure once it runs past the timeout and the interrupt's grace."""
        timeout = self._orchestrator_timeout
        await asyncio.wait([consuming], timeout=timeout)
        if consuming.done():
            return consuming.result()
        await self._orchestrator_gate.interrupt(session, turn)
        await asyncio.wait([consuming], timeout=min(INTERRUPT_GRACE, timeout))
        usage = consuming.result().usage if consuming.done() else TurnEnd("failed").usage
        return TurnEnd("failed", usage, f"the Orchestrator took longer than {timeout:g} s")

    async def _guarded(self, events: AsyncIterator[AgentEvent]) -> AsyncIterator[AgentEvent]:
        """A session that raises ends its turn as failed, so one bad turn never stops the Hub."""
        try:
            async for event in events:
                yield event
        except Exception as e:
            yield TurnEnd("failed", error=f"{type(e).__name__}: {e}")

    def _fail(self, task: Task, reason: str) -> None:
        task.fail(reason)
        message = event_message("hub", f"{task.id} failed: {reason}.", task.id)
        self._post(self._talker_inbox, _TalkerInput(message, "event", self._clock.now()))

    def _record(self, spec: SessionSpec, kind: str, end: TurnEnd, tools: list[str], **fields: Any) -> None:
        entry = {
            "kind": kind,
            "role": spec.role,
            "model": spec.model,
            "effort": spec.effort,
            **fields,
            "tools": tools,
            "outcome": end.outcome,
            "usage": asdict(end.usage),
        }
        try:
            self._metrics.record(entry)
        except Exception as e:
            self._output.notice(f"Could not write metrics: {e}")

    async def _delegate(self, args: dict[str, Any]) -> str:
        request = str(args.get("request") or "").strip()
        context = str(args.get("context") or "").strip()
        if not request:
            return "request is required."
        continues = bool(args.get("task_id"))
        if continues:
            task = self._tasks.get(args["task_id"])
            if task is None:
                return f"{no_task(args['task_id'])} Delegate without task_id to start a new one."
            if task.cancelled:
                return f"{task.id} was cancelled. Delegate without task_id to start a new one."
            task.words.append(request)
        else:
            task = self._tasks.create(request, self._clock.now())
        task.queue_message()
        message = task_message(task, request, context, continues=continues)
        self._post(self._orchestrator_inbox, _OrchestratorInput(task.id, message, self._clock.now()))
        return json.dumps({"task_id": task.id, "queued": True})

    async def _status(self, args: dict[str, Any]) -> str:
        return self._tasks.status(self._clock.now(), self._subagents.lines)

    async def _cancel(self, args: dict[str, Any]) -> str:
        task = self._tasks.get(args.get("task_id"))
        if task is None:
            return no_task(args.get("task_id"))
        if not task.cancel():
            return f"{task.id} is already {task.state}."
        for subagent in self._subagents.live(task.id):
            self._subagents.cancel(subagent.id)
        turn = self._orchestrator_gate.turn
        if self._orchestrating == task.id and turn is not None and self._orchestrator is not None:
            await self._orchestrator_gate.interrupt(self._orchestrator, turn)
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
            return no_task(args.get("task_id"))
        if task.id != self._orchestrating:
            return f"You are working on {self._orchestrating}: report on {self._orchestrating} only."
        if task.cancelled:
            return f"{task.id} was cancelled; the User will not hear this."
        spoken = str(args.get("spoken") or "").strip()
        if not spoken:
            return "spoken is required."
        details = str(args.get("details_markdown") or "").strip() or None
        needs_user = args.get("needs_user") in (True, "true")
        task.report(spoken, needs_user=needs_user)
        message = event_message("orchestrator", spoken, task.id, needs_user=needs_user, details=details is not None)
        self._post(self._talker_inbox, _TalkerInput(message, "event", self._clock.now(), details))
        return "Reported to the User."

    async def _spawn_subagent(self, args: dict[str, Any]) -> str:
        task = self._tasks.get(self._orchestrating)
        if task is None:
            return "spawn_subagent works only while you are on a Task."
        prepared = await self._subagents.prepare(task.id, args, task.words)
        if isinstance(prepared, Refused):
            return prepared.reason
        task.subagents += 1
        # A running Subagent keeps the Hub from settling, so text mode waits for its report.
        self._pending += 1
        self._settled.clear()
        state = self._subagents.start(prepared)
        if task.cancelled:
            self._subagents.cancel(prepared.id)
        return json.dumps(
            {
                "subagent_id": prepared.id,
                "state": state,
                "folder": str(prepared.cwd),
                "branch": prepared.branch,
                "log": str(prepared.log) if prepared.log else None,
                "note": "Its report reaches you as an event when it ends. End this turn without a report "
                "unless the User should hear something now.",
            }
        )

    async def _list_subagents(self, args: dict[str, Any]) -> str:
        return self._subagents.listing()

    async def _cancel_subagent(self, args: dict[str, Any]) -> str:
        return self._subagents.cancel(args.get("subagent_id"))

    def _subagent_ended(self, subagent: Subagent) -> None:
        try:
            task = self._tasks.get(subagent.task_id)
            if task is None:
                return
            task.subagents -= 1
            if not task.cancelled:
                task.queue_message()
                message = subagent_message(subagent, subagent.state, subagent.report)
                self._post(self._orchestrator_inbox, _OrchestratorInput(task.id, message, self._clock.now()))
            elif subagent.writes:
                # The Orchestrator hears nothing more of a cancelled Task, so the Hub says what its branch keeps.
                text = f"{subagent.id} on {task.id} stopped; its branch {subagent.branch} is kept, details on screen."
                message = event_message("hub", text, task.id, details=True)
                self._post(self._talker_inbox, _TalkerInput(message, "event", self._clock.now(), subagent.report))
        finally:
            self._handled()

    def _subagent_over_time(self, subagent: Subagent) -> None:
        task = self._tasks.get(subagent.task_id)
        if task is None or task.cancelled:
            return
        minutes = round((self._clock.now() - (subagent.started or subagent.created)) / 60)
        log = f" Its progress log is {subagent.log}." if subagent.log else ""
        text = (
            f"{subagent.id} has run {minutes} minutes and is still working.{log} Ask the User whether it should "
            "continue. It keeps going meanwhile; cancel_subagent stops it, and the Hub asks again after as long again."
        )
        task.queue_message()
        message = subagent_message(subagent, "over_time", text)
        self._post(self._orchestrator_inbox, _OrchestratorInput(task.id, message, self._clock.now()))
