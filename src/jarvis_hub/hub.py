"""The Hub core: turns User input into Talker turns, hands Tasks to the Orchestrator, and relays its reports.

Every outside dependency is injected (agent backend, clock, output, metrics, speech) so tests and text mode
share this one code path.
"""

import asyncio
import contextlib
import json
import re
import time
from collections import deque
from collections.abc import Mapping, AsyncIterator, Callable, Coroutine, Iterable
from dataclasses import asdict, dataclass, replace
from html import escape
from pathlib import Path
from typing import Any, Literal, Protocol

from jarvis_hub.agents.backend import (
    AgentBackend,
    AgentEvent,
    AgentSession,
    Context,
    Refused,
    SessionSpec,
    TextDelta,
    TextEnd,
    ToolCall,
    ToolHandler,
    TurnEnd,
)
from jarvis_hub.approvals import HUB_ACTIONS, Approval, Approvals, approval_event
from jarvis_hub.config import DEFAULT_TALKER_RESTART_AT
from jarvis_hub.limits import SavedLimit, UsageLimit, reset_sentence, update_sentence
from jarvis_hub.news import Leaf, News, fresh
from jarvis_hub.orchestrator import orchestrator_tools
from jarvis_hub.state import Fields, StateFile, UnreadableState
from jarvis_hub.spoken import SpokenScopes
from jarvis_hub.subagent_rules import connector_check
from jarvis_hub.subagents import Subagent, SubagentSetup, Subagents, ended_event, kept_line, over_time_event
from jarvis_hub.talker import talker_tools
from jarvis_hub.tasks import Task, Tasks, no_task
from jarvis_hub.voice.language import Lang, detect_language
from jarvis_hub.voice.speech import ClipName
from jarvis_hub.waits import Check, Watcher, Waits, WaitSetup, cancelled_event, check_event, watcher_event


class Clock(Protocol):
    def now(self) -> float:
        """Monotonic seconds."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class Output(Protocol):
    def heard(self, text: str, *, typed: bool = True) -> None:
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

    def begin(self, lang: Lang, *, turn: int, heard_at: float | None, owner: tuple[str, int] | None = None) -> None: ...

    def say(self, name: ClipName, lang: Lang, *, in_reply: bool = False) -> None: ...

    def feed(self, text: str) -> None: ...

    def block_end(self) -> None: ...

    def end(self) -> None: ...

    def stop(self) -> None: ...


class Floor(Protocol):
    """Voice mode's say over when the Talker may take a turn the User did not ask for (`VoiceMode`)."""

    async def quiet(self) -> None:
        """Returns once Jarvis is idle with nothing playing."""
        ...

    def owns(self, owner: tuple[str, int]) -> bool: ...

    def typed_input(self) -> None: ...

    def semantic_context(self, owner: tuple[str, int], start: int) -> None: ...

    def announce(self) -> bool:
        """Claims the voice for news if Jarvis is still idle, so a Wake word interrupts it; False if not idle."""
        ...


class _Silent:
    def begin(self, lang: Lang, *, turn: int, heard_at: float | None, owner: tuple[str, int] | None = None) -> None:
        pass

    def say(self, name: ClipName, lang: Lang, *, in_reply: bool = False) -> None:
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
    """A line the User typed or said, stamped when it was read or the User stopped speaking, not when the Hub got to it."""

    text: str
    arrived: float
    # Left out, the Hub detects it from the text.
    lang: Lang | None = None
    typed: bool = True
    # What the User heard of Jarvis's voice before cutting it off to say this; None when they did not cut it off.
    interrupted: str | None = None
    request_id: str | None = None
    revision: int = 0
    segments: tuple[str, ...] = ()
    relation_hint: str | None = None


# Sent once at launch, in the background, so the first real turn finds the prompt cached (ADR 0006); never shown.
WARM_UP = '<event source="hub">Session start. Reply with one word.</event>'

# The Phase 0 definition, kept so Hub numbers compare with the spike's: the end of a delta counts too.
_SENTENCE_END = re.compile(r"[.?!](\s|$)")

TalkerKind = Literal["warm_up", "restart", "user", "event"]

# How many of the latest exchanges a restarted Talker is told about.
RECENT_EXCHANGES = 6
# Turns a failed restart waits before trying again: each try costs a model call.
RESTART_RETRY_AFTER = 5
# How long a closing Hub lets a Talker open run on, so the session it opens can be closed and not left running.
OPEN_GRACE = 10.0

# An Orchestrator turn is a look-up of a few commands; one running this long is stuck.
ORCHESTRATOR_TIMEOUT = 600.0
# How long an interrupted turn has to end before the Hub gives up on its session.
INTERRUPT_GRACE = 10.0


def user_message(text: str, lang: Lang, *, typed: bool = True) -> str:
    # Escaped so the User's text can never close the tag or forge another one.
    marker = ' typed="true"' if typed else ""
    return f'<user lang="{lang}"{marker}>{escape(text, quote=False)}</user>'


def interrupted_message(spoken: str) -> str:
    # In a double-quoted attribute only `"` and markup need escaping; apostrophes stay as spoken.
    attribute = escape(spoken, quote=False).replace('"', "&quot;")
    return f'<interrupted basis="queue_estimate" spoken="{attribute}"/>'


def event_message(
    source: str, text: str, task: str, *, needs_user: bool = False, details: bool = False, approval: str | None = None
) -> str:
    on_screen = ' details="on screen"' if details else ""
    flag = "true" if needs_user else "false"
    asks = f' approval="{approval}"' if approval else ""
    return (
        f'<event source="{source}" task="{task}" needs_user="{flag}"{asks}{on_screen}>'
        f"{escape(text, quote=False)}</event>"
    )


def approval_brief(approval: Approval) -> str:
    """What a Subagent's brief carries of the Approval it acts on; the User's words are the Hub's record of them."""
    arguments = escape(json.dumps(approval.post_arguments), quote=False)
    pinned = f" once, with exactly these arguments: {arguments}" if approval.post else ", once"
    tools = f" It lets you call {', '.join(approval.tools)}{pinned}." if approval.tools else ""
    return (
        f'\n\n<approval id="{approval.id}">\n'
        f"The User approved exactly this, once: {escape(approval.text, quote=False)}.{tools}\n"
        f"Their words: {escape(str(approval.words), quote=False)}\n"
        "</approval>"
    )


def restart_message(open_tasks: str, exchanges: "deque[tuple[str, str]]") -> str:
    """The seed of a fresh Talker, written from the Hub's own records. The exchanges reuse the messages as sent."""
    recent = "\n".join(
        f"<exchange>\n{message}\n<you>{escape(reply, quote=False)}</you>\n</exchange>" for message, reply in exchanges
    )
    return (
        '<event source="hub" reason="restart">\n'
        "Your session was restarted to keep it fast, and what you said before it is gone. This is context for you, "
        "not the User speaking: do not answer it. Carry on from it in the User's next message.\n"
        f"<open_tasks>\n{open_tasks}\n</open_tasks>\n"
        f"<recent_exchanges>\n{recent or 'None yet.'}\n</recent_exchanges>\n"
        "Reply with one word.\n"
        "</event>"
    )


def _needs_user_key(spoken: str) -> str:
    return " ".join(spoken.casefold().split())


def task_message(task: Task, request: str, context: str, *, continues: bool) -> str:
    opening = f'<task id="{task.id}" continues="true">' if continues else f'<task id="{task.id}">'
    parts = [opening, f"<request>{escape(request, quote=False)}</request>"]
    if context:
        parts.append(f"<context>{escape(context, quote=False)}</context>")
    return "\n".join([*parts, "</task>"])


@dataclass(frozen=True)
class _TalkerInput:
    kind: TalkerKind
    arrived: float
    # The User's line as the Talker gets it; empty for news.
    said: str = ""
    heard: str | None = None
    lang: Lang | None = None
    typed: bool = True
    spoken: bool = True
    request_id: str | None = None
    revision: int = 0
    # The news this turn tells: all of a news turn, or the away opener a User's spoken turn opens with.
    news: News | None = None

    @property
    def message(self) -> str:
        return "\n".join(part for part in (self.news.message if self.news else "", self.said) if part)

    @property
    def details(self) -> str | None:
        return self.news.details if self.news else None

    @property
    def count(self) -> int:
        """How many posted items this turn carries, each handled once it ends."""
        return (1 if self.kind == "user" else 0) + (self.news.count if self.news else 0)


def _told(news: News) -> _TalkerInput:
    return _TalkerInput("event", news.arrived, news=news)


def recovery_message(
    open_tasks: str, interrupted: list[Subagent], unanswered: "list[_OrchestratorInput]", problems: list[str]
) -> str:
    """The Orchestrator's one message after a restart, listing what was pending when the old Hub stopped; what to
    do with it is in the Orchestrator's prompt."""
    parts = ['<event source="hub" reason="restart">', f"<open_tasks>\n{open_tasks}\n</open_tasks>"]
    for subagent in interrupted:
        where = f"branch {subagent.branch}" if subagent.branch else "read-only"
        parts.append(
            f'<interrupted id="{subagent.id}" task="{subagent.task_id}" repo="{subagent.repo}">'
            f"{escape(f'{subagent.title} ({where}). {subagent.report}', quote=False)}</interrupted>"
        )
    # Each message is one the Hub built, its contents already escaped.
    parts += [f'<unanswered task="{item.task_id}">{item.message}</unanswered>' for item in unanswered]
    parts += [f"<not_restored>{escape(p, quote=False)}</not_restored>" for p in problems]
    return "\n".join([*parts, "</event>"])


@dataclass(frozen=True)
class _OrchestratorInput:
    task_id: str
    message: str
    arrived: float
    # The message is a Check's last firing: no later event will wake the Orchestrator for it.
    last_check: bool = False
    # The recovery message after a restart, on these Tasks at once.
    restart: tuple[str, ...] = ()
    # The messages the old Hub left unanswered, which the recovery message carries.
    carried: "tuple[_OrchestratorInput, ...]" = ()
    revision: int = 0


# What the state file keeps of a message the Orchestrator has not answered yet.
_UNANSWERED = Fields(("task_id", "message", "arrived", "last_check", "revision"), times=frozenset({"arrived"}))


class _TurnGate:
    """Lets an interrupt land only on the turn it was meant for, never on the one after."""

    def __init__(self) -> None:
        self.turn: int | None = None
        self._lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Task[bool]] = {}

    def request_interrupt(self, session: AgentSession, turn: int) -> asyncio.Task[bool]:
        if turn not in self._pending:
            task = asyncio.create_task(self.interrupt(session, turn))
            self._pending[turn] = task
            task.add_done_callback(lambda done: self._pending.pop(turn, None))
        return self._pending[turn]

    async def close(self) -> None:
        pending = tuple(self._pending.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def interrupt(self, session: AgentSession, turn: int) -> bool:
        if self.turn != turn:
            return False
        async with self._lock:
            await session.interrupt()
        return True

    async def before_next_turn(self) -> None:
        if self._pending:
            await asyncio.gather(*self._pending.values(), return_exceptions=True)
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
        waits: WaitSetup,
        orchestrator_timeout: float = ORCHESTRATOR_TIMEOUT,
        speech: Speech | None = None,
        speak_typed: bool = True,
        on_end_conversation: Callable[[], None] | None = None,
        on_model_ready: Callable[[], None] | None = None,
        talker_restart_at: float = DEFAULT_TALKER_RESTART_AT,
        floor: Floor | None = None,
        state_path: Path | None = None,
        wall: Callable[[], float] = time.time,
        preconnect_orchestrator: bool = False,
    ) -> None:
        tools = talker_tools(
            delegate=self._delegate,
            status=self._status,
            cancel=self._cancel,
            show=self._show,
            end_conversation=self._end_conversation,
            quiet_mode=self._quiet_mode_tool,
            approve=self._approve,
            resolve_scope=self._resolve_scope,
        )
        self._talker_spec = replace(talker, tools=tuple(replace(tool, handler=self._owned_tool(tool.handler)) for tool in tools))
        self._orchestrator_spec = replace(
            orchestrator,
            tools=orchestrator_tools(
                report=self._report,
                spawn_subagent=self._spawn_subagent,
                list_subagents=self._list_subagents,
                cancel_subagent=self._cancel_subagent,
                watch=self._watch,
                schedule_check=self._schedule_check,
                cancel_wait=self._cancel_wait,
            ),
        )
        self._orchestrator_spec = replace(self._orchestrator_spec, tools=tuple(
            replace(tool, handler=self._task_tool(tool.handler)) for tool in self._orchestrator_spec.tools
        ))
        self._backend = backend
        self._clock = clock
        self._output = output
        self._metrics = metrics
        self._orchestrator_timeout = orchestrator_timeout
        self._speech: Speech = speech if speech is not None else _Silent()
        # In voice mode a typed line gets a text-only reply; with `--text --speak` it is spoken.
        self._speak_typed = speak_typed
        self._on_end_conversation = on_end_conversation
        self._on_model_ready = on_model_ready
        self._model_available = False
        self._restart_at = talker_restart_at
        # In voice mode a restart and news wait for Jarvis to be quiet, so they never delay or talk over the User.
        self._floor = floor
        # The exchanges a restarted Talker is seeded with.
        self._exchanges: deque[tuple[str, str]] = deque(maxlen=RECENT_EXCHANGES)
        # The language of the latest line read, and of the latest User turn the Talker took: replies follow it.
        self._read_lang: Lang = "en"
        self._lang: Lang = "en"
        self._tasks = Tasks()
        self._talker: AgentSession | None = None
        # Turns left before a failed restart is tried again, and whether the User has heard of the failure.
        self._restart_wait = 0
        self._restart_failing = False
        self._orchestrator: AgentSession | None = None
        self._talker_inbox: asyncio.Queue[_TalkerInput] = asyncio.Queue()
        # Voice mode's news, in arrival order, for when Jarvis is quiet; text mode puts it in the inbox.
        self._news: deque[News] = deque()
        # Quiet mode, and the news it holds for the next Wake word.
        self._quiet_mode = False
        self._held: list[Leaf] = []
        # Per Task, the needs_user news last let through: the same again waits out Quiet mode.
        self._needs_user: dict[str, str] = {}
        # Held news on the Talker in the turn under way, saved until that turn ends.
        self._telling: tuple[Leaf, ...] = ()
        # Set whenever a line or news is posted for the Talker.
        self._arrived = asyncio.Event()
        # One queue, so the Orchestrator's single session takes one message at a time.
        self._orchestrator_inbox: asyncio.Queue[_OrchestratorInput] = asyncio.Queue()
        self._loops: list[asyncio.Task[None]] = []
        self._pending = 0
        self._settled = asyncio.Event()
        self._settled.set()
        # Once the input has ended voice mode stops stepping, so the voice no longer gates news or a restart.
        self._input_ended = asyncio.Event()
        self._turn_count = 0
        self._active_spoken_owner: tuple[str, int] | None = None
        self._turn_task_revisions: dict[str, int] = {}
        self._producer_revisions: dict[str, int] = {}
        self._persist_suspended = False
        self._talker_gate = _TurnGate()
        # Whether the Talker turn under way is heard: only a spoken turn's reply is interrupted.
        self._turn_spoken = False
        # Only the reply to the User's own spoken turn ends the conversation; news never does.
        self._turn_answers_user = False
        self._interrupted_turn: int | None = None
        self._local_interrupts: set[asyncio.Task[bool]] = set()
        self._orchestrator_gate = _TurnGate()
        # The Tasks of the running Orchestrator turn: one, or every open one in the recovery turn after a restart.
        self._turn_tasks: tuple[str, ...] = ()
        # Tasks the running Orchestrator turn started Subagents on: their completion events bring the next turn.
        self._spawned: set[str] = set()
        self._state = StateFile(state_path) if state_path is not None else None
        # Persisted times are wall-clock; the Hub's own clock is monotonic and restarts with the process.
        self._wall = wall
        self._closing = False
        self._save_failing = False
        # Subagents stopped by this Hub's close, for the next launch's recovery message.
        self._interrupted: list[str] = []
        # The Orchestrator's messages from posting until their turn ends, saved so a restart loses none.
        self._unanswered: list[_OrchestratorInput] = []
        self._limit = UsageLimit(clock, wall, self._limit_hit, self._limit_cleared, self._limit_reset)
        # Held until the usage limit resets, each still counted in `_pending` so a text-mode run waits for it: the
        # Orchestrator's messages, which stay unanswered and so saved, and the news the limited Talker could not take.
        self._deferred: list[_OrchestratorInput] = []
        self._parked_news: list[News] = []
        self._announcing: set[asyncio.Task[None]] = set()
        # Whether a Talker reply is on screen or being said.
        self._replying = False
        # The User's own text in the Talker turn under way, for the Tasks it delegates.
        self._user_text: str | None = None
        self._scopes = SpokenScopes(clock, change=self._persist, notice=output.notice)
        self._active_scope_authority: dict[str, tuple[int, int]] = {}
        self._active_scope_id: str | None = None
        self._scope_news: list[Leaf] = []
        self._scope_inputs: list[_OrchestratorInput] = []
        self._scope_queued: dict[str, Subagent] = {}
        self._scope_waits: set[str] = set()
        self._approvals = Approvals(
            org=waits.org, runner=waits.runner, clock=clock, on_change=self._persist, log=self._write
        )
        self._preconnect_orchestrator = preconnect_orchestrator
        # What the Talker turn under way has shown so far, for the Approvals it names.
        self._shown = ""
        self._carrying_out: set[asyncio.Task[None]] = set()
        self._approval_runs: dict[str, asyncio.Task[None]] = {}
        self._session_for = subagents.session
        self._subagents = Subagents(
            setup=replace(subagents, session=self._subagent_session),
            backend=backend,
            clock=clock,
            on_end=self._subagent_ended,
            on_over_time=self._subagent_over_time,
            on_turn=self._subagent_turn,
            limit=self._limit,
            # Saved at once: a crash must not leave it saved as queued, which the next launch would start again.
            on_start=lambda subagent: self._persist(),
        )
        self._waits = Waits(setup=waits, clock=clock, on_watcher_end=self._watcher_ended, on_check=self._check_due)

    async def start(self) -> None:
        """Open the Talker and warm it in the background (ADR 0006); a line typed meanwhile waits for it.
        What the last launch left pending is restored first, and the Orchestrator hears of it in the background."""
        recovering = self._restore()
        self._loops = [asyncio.create_task(self._talk()), asyncio.create_task(self._orchestrate())]
        if recovering is not None:
            self._loops.append(asyncio.create_task(recovering))

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
        self._input_ended.set()
        self._release_held()
        settling = asyncio.create_task(self._settled.wait())
        await asyncio.wait([settling, talking], return_when=asyncio.FIRST_COMPLETED)
        settling.cancel()

    def request_interrupt(self) -> asyncio.Task[bool] | None:
        self._speech.stop()
        turn = self._talker_gate.turn
        if turn is None or self._talker is None or not self._turn_spoken:
            return None
        self._interrupted_turn = turn
        task = self._talker_gate.request_interrupt(self._talker, turn)
        if task not in self._local_interrupts:
            self._local_interrupts.add(task)
            task.add_done_callback(self._local_stop_done)
        return task

    def _local_stop_done(self, task: asyncio.Task[bool]) -> None:
        self._local_interrupts.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._output.notice("Audio stopped locally; the generation stop was not confirmed. Check the model connection; typed input remains available.")

    async def interrupt(self) -> bool:
        """Stop whatever is being said, and the Talker turn under way if its reply is spoken; a typed line's reply
        goes on. Returns whether a Talker turn was interrupted."""
        self._speech.stop()
        turn = self._talker_gate.turn
        if turn is None or self._talker is None or not self._turn_spoken:
            return False
        self._interrupted_turn = turn
        return await self._talker_gate.interrupt(self._talker, turn)

    async def close(self) -> None:
        """Stops everything; Tasks, their waits and the Subagents it interrupts are saved for the next launch."""
        self._closing = True
        await self._talker_gate.close()
        await self._scopes.close()
        await self._waits.close()
        await self._subagents.close()
        await self._limit.close()
        for announcing in [*self._announcing, *self._carrying_out]:
            announcing.cancel()
        if self._carrying_out:
            await asyncio.wait(self._carrying_out)
        self._save()
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

    def admit_spoken_revision(self, line: Line) -> None:
        if line.request_id is None:
            return
        self._persist_suspended = True
        try:
            scope = self._scopes.admit(line.request_id, line.revision, line.segments or (line.text,), line.arrived)
            for task_id in scope.task_ids:
                task = self._tasks.get(task_id)
                if task is not None:
                    scope.task_context[task_id] = f"{task.request} (state: {task.state})" + (
                        f" Last report: {task.last_report}" if task.last_report else "")
            self._park_scope_news()
        finally:
            self._persist_suspended = False
        self._persist()

    async def _read(self, inputs: AsyncIterator[Line]) -> None:
        async for line in inputs:
            if text := line.text.strip():
                if line.typed and self._floor is not None:
                    self._floor.typed_input()
                    await self.interrupt()
                if line.typed:
                    self._scopes.typed(text)
                if line.request_id is not None:
                    owner = (line.request_id, line.revision)
                    if self._floor is not None and not self._floor.owns(owner):
                        continue
                    self.admit_spoken_revision(line)
                    if self._floor is not None and not self._floor.owns(owner):
                        continue
                self._read_lang = lang = line.lang or detect_language(text, self._read_lang)
                message = user_message(text, lang, typed=line.typed)
                if line.interrupted is not None:
                    message = f"{interrupted_message(line.interrupted)}\n{message}"
                spoken = not line.typed or self._speak_typed
                item = _TalkerInput("user", line.arrived, message, heard=text, lang=lang, typed=line.typed, spoken=spoken,
                                    request_id=line.request_id, revision=line.revision)
                self._post(self._talker_inbox, item)

    def _post(self, inbox: "asyncio.Queue[Any]", item: _TalkerInput | _OrchestratorInput) -> None:
        """Queue an item; the Hub counts as settled again once every posted item is handled."""
        self._pending += 1
        self._settled.clear()
        inbox.put_nowait(item)
        self._arrived.set()
        self._persist()

    def _ask(self, item: _OrchestratorInput) -> None:
        self._unanswered.append(item)
        self._post(self._orchestrator_inbox, item)

    def _post_news(
        self,
        source: str,
        text: str,
        task: str,
        *,
        needs_user: bool = False,
        details: str | None = None,
        approval: str | None = None,
    ) -> None:
        message = event_message(
            source, text, task, needs_user=needs_user, details=details is not None, approval=approval
        )
        urgent = False
        if self._floor is not None:
            key = _needs_user_key(text)
            urgent = needs_user and self._needs_user.get(task) != key
            if needs_user:
                self._needs_user[task] = key
            else:
                self._needs_user.pop(task, None)
        recorded_task = self._tasks.get(task)
        self._queue_news(
            Leaf(message, self._clock.now(), f"{task}: {text}", details, urgent=urgent, approval=approval,
                 task_id=task or None, revision=recorded_task.revision if recorded_task is not None else 0)
        )

    def _queue_news(self, leaf: Leaf) -> None:
        """News for the Talker: in voice mode an Announcement once Jarvis is quiet, or held while Quiet mode is on."""
        if leaf.task_id is not None and self._scopes.held(leaf.task_id):
            self._scope_news.append(leaf)
            self._persist()
            return
        if self._floor is None:
            self._post(self._talker_inbox, _told(News.one(leaf)))
            return
        self._pending += 1
        self._settled.clear()
        self._queue(News.one(leaf))
        self._arrived.set()
        self._persist()

    def _park_scope_news(self) -> None:
        held = self._scopes.held_tasks()
        dropped = 0
        def take(items: Iterable[News]) -> list[News]:
            nonlocal dropped
            remaining: list[News] = []
            for news in items:
                leaves = [leaf for leaf in news.leaves() if leaf.task_id in held]
                self._scope_news.extend(leaves)
                dropped += len(leaves)
                if rest := news.without_tasks(held):
                    remaining.append(rest)
            return remaining
        self._news = deque(take(self._news))
        self._parked_news = take(self._parked_news)
        leaves = [leaf for leaf in self._held if leaf.task_id in held]
        self._scope_news.extend(leaves)
        self._held = [leaf for leaf in self._held if leaf.task_id not in held]
        dropped += len(leaves)
        for _ in range(dropped):
            self._handled()

    def _release_scope_news(self) -> None:
        waiting, self._scope_news = self._scope_news, []
        for leaf in waiting:
            task = self._tasks.get(leaf.task_id)
            if task is not None and leaf.revision != task.revision:
                continue
            if leaf.task_id is not None and self._scopes.held(leaf.task_id):
                self._scope_news.append(leaf)
            else:
                self._queue_news(leaf)
        self._persist()

    def _queue(self, news: News) -> None:
        """News waits for Jarvis to be quiet; in Quiet mode only what gets through it waits, and the rest is held."""
        if self._quiet_mode:
            through, held = news.split()
            self._held += held
            if through is None:
                return
            news = through
        self._news.append(news)

    def woke(self) -> None:
        """A Wake word opened listening: it ends Quiet mode, and the news it held opens the User's next turn."""
        self._release_held()

    def _release_held(self) -> None:
        """Ends Quiet mode. What it held goes first in the news, as one "while you were away" item."""
        self._quiet_mode = False
        if not self._held:
            self._persist()
            return
        held, self._held = self._held, []
        self._news.appendleft(News.away(held, self._clock.now()))
        self._arrived.set()
        self._persist()

    def _drop_reset_news(self) -> None:
        """A new hit makes the news that the limit reset untrue: it is dropped wherever it waits, held, in the news
        or parked, and no longer counted as pending or among the updates."""
        dropped = len(self._held)
        self._held = fresh(self._held)
        dropped -= len(self._held)
        news, from_news = self._less_reset(self._news)
        parked, from_parked = self._less_reset(self._parked_news)
        self._news, self._parked_news = deque(news), parked
        dropped += from_news + from_parked
        if not dropped:
            return
        self._pending -= dropped
        if self._pending == 0:
            self._settled.set()
        self._persist()

    @staticmethod
    def _less_reset(items: "Iterable[News]") -> tuple[list[News], int]:
        """`items` less the news that the limit reset, and how many posted items that took out."""
        kept: list[News] = []
        dropped = 0
        for item in items:
            rest = item.without_reset()
            dropped += item.count - (rest.count if rest else 0)
            if rest:
                kept.append(rest)
        return kept, dropped

    def _with_away(self, item: _TalkerInput) -> _TalkerInput:
        """The User's first spoken turn after the Wake word that ended Quiet mode opens with what it held."""
        if item.kind != "user" or item.typed or not self._news:
            return item
        away = self._news[0]
        if not away.is_away or away.arrived > item.arrived:
            return item
        self._news.popleft()
        return replace(item, news=away)

    def _handled(self) -> None:
        self._pending -= 1
        if self._pending == 0:
            self._settled.set()
        self._persist()

    def _persist(self) -> None:
        """Saves the Hub's records as they change; a closing Hub saves once, at the end of close()."""
        if self._persist_suspended:
            return
        if not self._closing:
            self._save()

    def _save(self) -> None:
        if self._state is None:
            return
        now, wall = self._clock.now(), self._wall()

        def to_wall(t: float) -> float:
            return wall - (now - t)

        tasks = self._tasks.dump(to_wall, include=self._scopes.held_tasks() | self._subagents.unfinished_task_ids())
        ids = {t["id"] for t in tasks}
        data = {
            "saved_at": wall,
            "spoken_scopes": self._scopes.dump(to_wall),
            "scope_news": [leaf.dump(to_wall) for leaf in self._scope_news],
            "tasks": tasks,
            "subagents": self._subagents.dump(ids, to_wall),
            "waits": [w for w in self._waits.dump(to_wall) if w["task_id"] in ids],
            "interrupted": self._interrupted,
            "unanswered": [_UNANSWERED.dump(i, to_wall) for i in self._unanswered if i.task_id in ids],
            "quiet_mode": self._quiet_mode,
            "held": [leaf.dump(to_wall) for leaf in self._unheard()],
            "needs_user": self._needs_user,
            "usage_limit": self._limit.dump(),
            "parked": [news.dump(to_wall) for news in self._parked_news],
            "approvals": self._approvals.dump(ids, to_wall),
        }
        try:
            self._state.save(data)
        except OSError as e:
            if not self._save_failing:
                self._output.notice(f"Could not save the Hub's state, so a restart would lose it: {e}")
            self._save_failing = True
        else:
            self._save_failing = False

    def _unheard(self) -> list[Leaf]:
        """News Quiet mode held that the User has not heard: in the turn under way, waiting in the news, or held."""
        waiting = [leaf for news in self._news for leaf in news.away_leaves()]
        return [*self._telling, *waiting, *self._held]

    def _restore_quiet_mode(self, held: list[Leaf], quiet_mode: bool, needs_user: dict[str, str]) -> None:
        """Brings back Quiet mode and its news; news released before the restart but not yet heard goes out again."""
        if self._floor is None:
            for leaf in held:
                self._post(self._talker_inbox, _told(News.one(leaf)))
            return
        self._needs_user = needs_user
        self._held = held
        self._pending += len(held)
        if held:
            self._settled.clear()
        if quiet_mode:
            self._quiet_mode = True
        else:
            self._release_held()

    def _restore_limit(self, limit: SavedLimit, parked: list[News]) -> None:
        """Restores holds and parked news without persisting the partially restored Hub."""
        self._parked_news = parked
        self._pending += sum(item.count for item in parked)
        if parked:
            self._settled.clear()
        self._limit.restore(limit)
        # Restored holds are visible before a new user request.
        if self._limit.active:
            self._limit_notice(self._limit.tell())

    def _restore_held(self, held: list[Leaf], quiet_mode: bool, needs_user: dict[str, str]) -> None:
        """Quiet mode and its news, then the parked news if the Talker is no longer limited, under its rules."""
        self._restore_quiet_mode(held, quiet_mode, needs_user)
        if self._parked_news and not self._limit.blocks("talker"):
            self._unpark()

    def _restore(self) -> "Coroutine[Any, Any, None] | None":
        """Brings back what the last launch saved; returns the recovery to run in the background, if any."""
        if self._state is None:
            return None
        try:
            data = self._state.load()
        except UnreadableState as e:
            self._output.notice(str(e))
            return None
        if data is None:
            return None
        now, wall = self._clock.now(), self._wall()

        def from_wall(t: float) -> float:
            return now - (wall - t)

        try:
            tasks = Tasks.load(data["tasks"], from_wall)
            subagents = Subagents.load(data["subagents"], from_wall)
            waits, problems = self._waits.load(data["waits"], from_wall)
            unanswered = [_OrchestratorInput(**_UNANSWERED.load({"revision": 0, **r}, from_wall)) for r in data["unanswered"]]
            told = [str(i) for i in data["interrupted"]]
            held = [Leaf.load(r, from_wall) for r in data["held"]]
            quiet_mode = data["quiet_mode"] is True
            needs_user = {str(task): str(key) for task, key in dict(data["needs_user"]).items()}
            limit = UsageLimit.load(data["usage_limit"])
            parked = [News.load(r, from_wall) for r in data["parked"]]
            approvals = Approvals.load(data["approvals"], from_wall)
            loaded_scopes = SpokenScopes.load(data.get("spoken_scopes", []), from_wall)
            scope_news = [Leaf.load(record, from_wall) for record in data.get("scope_news", [])]
        except (KeyError, TypeError, ValueError, IndexError, AttributeError) as e:
            self._output.notice(self._state.set_aside(f"could not be read whole ({type(e).__name__}: {e})"))
            return None
        self._scopes.install(loaded_scopes)
        self._scope_news = scope_news
        self._restore_limit(limit, parked)
        if not tasks and not self._scopes.pending():
            self._restore_held(held, quiet_mode, needs_user)
            return None
        self._tasks.restore(tasks)
        def current(task_id: str, revision: int) -> bool:
            task = self._tasks.get(task_id)
            return task is not None and task.revision == revision
        unanswered = [item for item in unanswered if current(item.task_id, item.revision)]
        waits = [wait for wait in waits if current(wait.task_id, wait.task_revision)]
        for approval in approvals:
            if not current(approval.task_id, approval.task_revision):
                approval.state, approval.outcome = "withdrawn", "Its spoken request was revised."
        for subagent in subagents:
            self._producer_revisions[subagent.id] = subagent.task_revision
            if not current(subagent.task_id, subagent.task_revision) and subagent.state == "queued":
                subagent.state = "running"
        for wait in waits:
            self._producer_revisions[wait.id] = wait.task_revision
        unfinished, dropped = self._approvals.restore(approvals)
        for notice in dropped:
            self._output.notice(notice)
        cut_off = self._subagents.restore(subagents)
        for subagent in cut_off:
            self._approvals.subagent_ended(subagent.id)
        queued = self._subagents.queued()
        self._scope_queued = {subagent.id: subagent for subagent in queued if self._scopes.held(subagent.task_id)}
        queued = [subagent for subagent in queued if subagent.id not in self._scope_queued]
        self._waits.restore(waits)
        self._scope_waits = {wait.id for wait in waits if self._scopes.held(wait.task_id)}
        self._scope_inputs = [item for item in unanswered if self._scopes.held(item.task_id)]
        self._unanswered = unanswered
        interrupted = [s for s in map(self._subagents.get, told) if s is not None]
        self._interrupted = [s.id for s in interrupted]
        # After the rest, since releasing held news saves the state file.
        self._restore_held(held, quiet_mode, needs_user)
        # A restored Watcher or queued Subagent ends on its own, and the recovery message is news still to come.
        self._pending += 1 + sum(isinstance(w, Watcher) and w.id not in self._scope_waits for w in waits) + len(queued) + len(unfinished)
        self._settled.clear()
        relevant = [subagent for subagent in interrupted + cut_off
                    if current(subagent.task_id, subagent.task_revision)]
        return self._recover(tasks, cut_off, relevant, queued, problems, unfinished)

    async def _recover(
        self,
        tasks: list[Task],
        cut_off: list[Subagent],
        interrupted: list[Subagent],
        queued: list[Subagent],
        problems: list[str],
        unfinished: list[Approval],
    ) -> None:
        """Frees the worktrees of Subagents a crash cut off, tells the Orchestrator what was pending, and only then
        starts the restored waits and queued Subagents, so neither reaches it before the recovery message."""
        try:
            for subagent in cut_off:
                await self._subagents.free_interrupted(subagent)
            pending_scopes = self._scopes.pending()
            if pending_scopes:
                text = "\n\n".join(scope.text for scope in pending_scopes)
                self._post(self._talker_inbox, _TalkerInput("user", self._clock.now(),
                    user_message(text, self._lang, typed=False), heard=text, typed=False, spoken=False))
            ids = tuple(task.id for task in tasks if task.open and not self._scopes.held(task.id))
            if ids:
                listing = self._tasks.open_status(self._clock.now(), self._task_lines)
                carried = tuple(item for item in self._unanswered if not self._scopes.held(item.task_id))
                available_interrupted = [subagent for subagent in interrupted if not self._scopes.held(subagent.task_id)]
                message = recovery_message(listing, available_interrupted, list(carried), problems)
                self._ask(_OrchestratorInput("", message, self._clock.now(), restart=ids, carried=carried))
            self._interrupted = []
        finally:
            self._waits.resume(only={wait.id for task in tasks for wait in self._waits.live(task.id)
                                     if wait.id not in self._scope_waits})
            # Each still waits for the usage limit, when the restored one holds.
            for subagent in queued:
                self._subagents.start(subagent)
            # Already counted as pending.
            for approval in unfinished:
                self._carry_out(approval)
            self._handled()

    async def _talk(self) -> None:
        try:
            self._talker = await self._open_talker()
            await self._turn(WARM_UP, kind="warm_up", arrived=self._clock.now())
        except Exception as e:
            self._output.notice(f"Model unavailable: could not start the Talker: {e}. Check Claude sign-in or connection, then restart Jarvis.")
            return
        upcoming: _TalkerInput | None = None
        while True:
            if upcoming is None:
                item = await self._next_input()
            else:
                item, upcoming = upcoming, None
                await self._talker_gate.before_next_turn()
            item = self._with_away(item)
            if item.news is not None:
                held = self._scopes.held_tasks()
                moved = [leaf for leaf in item.news.leaves() if leaf.task_id in held]
                if moved:
                    self._scope_news.extend(moved)
                    for _ in moved:
                        self._handled()
                    item = replace(item, news=item.news.without_tasks(held))
                    if item.kind == "event" and item.news is None:
                        continue
            if item.news is not None:
                stale = {leaf.task_id for leaf in item.news.leaves() if leaf.task_id is not None
                         and (task := self._tasks.get(leaf.task_id)) is not None and leaf.revision != task.revision}
                rest = item.news.without_tasks(stale)
                for _ in range(item.news.count - (rest.count if rest else 0)):
                    self._handled()
                item = replace(item, news=rest)
                if item.kind == "event" and rest is None:
                    continue
            # How many of the item's posted items wait for the usage limit to reset: they stay pending.
            parked = 0
            try:
                if item.lang:
                    self._lang = item.lang
                if item.heard:
                    self._output.heard(item.heard, typed=item.typed)
                if item.kind == "event" and self._limit.blocks("talker"):
                    parked = self._park(item)
                    continue
                if item.details:
                    self._output.show(item.details)
                self._user_text = item.heard if item.kind == "user" else None
                self._telling = item.news.away_leaves() if item.news else ()
                self._approvals.turn_began(self._user_text)
                self._shown = ""
                if item.kind == "user" and self._limit.blocks("talker"):
                    end = self._limited_reply(item)
                else:
                    owner = (item.request_id, item.revision) if item.request_id is not None else None
                    if owner is not None and self._floor is not None and not self._floor.owns(owner):
                        end = TurnEnd("interrupted")
                    else:
                        authority = self._scopes.activate() if item.kind == "user" else {}
                        scope_context = self._scopes.context() if authority else ""
                        message = "\n".join(part for part in (item.message, scope_context) if part)
                        end = await self._turn(message, kind=item.kind, arrived=item.arrived, spoken=item.spoken,
                                               owner=owner, scope_authority=authority)
                told = [leaf.approval for leaf in (item.news.leaves() if item.news else ()) if leaf.approval]
                self._approvals.turn_ended(completed=end.outcome == "completed", told=told, said=self._shown)
                if end.outcome == "limited":
                    # The news was not told, so the Talker gets it again after the reset.
                    parked = self._park(item)
            finally:
                self._user_text = None
                self._telling = ()
                for _ in range(item.count - parked):
                    self._handled()
                if parked:
                    self._persist()
                self._scopes.prune()
            upcoming = await self._restart_if_full(end.context)

    async def _next_input(self) -> _TalkerInput:
        """The User's next line first; in voice mode, news only once Jarvis is quiet (the announcement policy), or
        on screen once the input has ended.

        From the `announce` claim to the turn's start nothing awaits, so a Wake word lands before the claim, which
        then fails, or after it, on the news turn.
        """
        if self._floor is None:
            await self._talker_gate.before_next_turn()
            return await self._talker_inbox.get()
        quiet = False
        while True:
            await self._talker_gate.before_next_turn()
            if not self._talker_inbox.empty():
                return self._talker_inbox.get_nowait()
            if self._news and self._input_ended.is_set():
                return replace(_told(self._news.popleft()), spoken=False)
            if self._news and quiet and self._floor.announce():
                # Everything waiting goes as one Announcement rather than one Follow-up window each.
                news = News.joined(self._news)
                self._news.clear()
                return _told(news)
            quiet = await self._line_or_quiet()

    async def _line_or_quiet(self) -> bool:
        """Waits for a User line, or for news and Jarvis being quiet; returns whether Jarvis was quiet."""
        line = asyncio.ensure_future(self._a_line())
        quiet = asyncio.ensure_future(self._news_and_quiet())
        try:
            await asyncio.wait([line, quiet], return_when=asyncio.FIRST_COMPLETED)
        finally:
            line.cancel()
            quiet.cancel()
        return quiet.done() and not quiet.cancelled()

    async def _a_line(self) -> None:
        while self._talker_inbox.empty():
            self._arrived.clear()
            await self._arrived.wait()

    async def _news_and_quiet(self) -> None:
        while not self._news:
            self._arrived.clear()
            await self._arrived.wait()
        await self._quiet()

    async def _quiet(self) -> None:
        """Returns once voice mode is quiet, or the input has ended."""
        assert self._floor is not None
        quiet = asyncio.ensure_future(self._floor.quiet())
        ended = asyncio.ensure_future(self._input_ended.wait())
        try:
            await asyncio.wait([quiet, ended], return_when=asyncio.FIRST_COMPLETED)
        finally:
            quiet.cancel()
            ended.cancel()

    async def _open_talker(self) -> AgentSession:
        """Open a Talker session. A cancel that lands meanwhile lets the open finish and closes what it opened."""
        opening = asyncio.ensure_future(self._backend.open(self._talker_spec))
        try:
            return await asyncio.shield(opening)
        except asyncio.CancelledError:
            done, _ = await asyncio.wait([opening], timeout=OPEN_GRACE)
            if not done:
                opening.cancel()
            elif not opening.cancelled() and opening.exception() is None:
                with contextlib.suppress(Exception):
                    await opening.result().close()
            raise

    async def _restart_if_full(self, context: Context | None) -> _TalkerInput | None:
        """Between turns, swap in a fresh Talker once the context passes the threshold (ADR 0006's warm-up, again).

        Only this turn's context decides: an older reading may be stale. The new session reads the seed in the
        background like the launch warm-up, so a line typed meanwhile waits for it in the inbox. Whatever fails
        leaves the old session talking, and the restart waits RESTART_RETRY_AFTER turns before trying again.
        In voice mode the restart first waits for Jarvis to go quiet; an item reaching the inbox before then puts
        it off to after that item's turn, and is returned for the loop to take next.
        """
        if self._restart_wait:
            self._restart_wait -= 1
            return None
        if context is None or context.used < self._restart_at:
            return None
        if (upcoming := await self._quiet_or_next()) is not None:
            return upcoming
        old = self._talker
        try:
            session = await self._open_talker()
        except Exception as e:
            self._restart_failed(str(e))
            return None
        try:
            seed = restart_message(self._tasks.open_status(self._clock.now(), self._task_lines), self._exchanges)
            end = await self._turn(seed, kind="restart", arrived=self._clock.now(), session=session)
        except BaseException:
            with contextlib.suppress(Exception):
                await session.close()
            raise
        if end.outcome != "completed":
            with contextlib.suppress(Exception):
                await session.close()
            self._restart_failed(end.error or end.outcome)
            return None
        self._talker, self._restart_failing = session, False
        if old is not None:
            with contextlib.suppress(Exception):
                await old.close()
        return None

    async def _quiet_or_next(self) -> _TalkerInput | None:
        """Waits for voice mode to go quiet (or the input to end), unless the inbox's next item comes first; then
        returns that item."""
        if self._floor is None:
            return None
        quiet = asyncio.ensure_future(self._quiet())
        getting = asyncio.ensure_future(self._talker_inbox.get())
        try:
            await asyncio.wait([quiet, getting], return_when=asyncio.FIRST_COMPLETED)
        finally:
            quiet.cancel()
            if not getting.done():
                getting.cancel()
        return getting.result() if getting.done() and not getting.cancelled() else None

    def _restart_failed(self, error: str) -> None:
        self._restart_wait = RESTART_RETRY_AFTER
        if not self._restart_failing:
            self._output.notice(f"The Talker could not restart, so the old session goes on: {error}")
        self._restart_failing = True

    async def _turn(
        self,
        message: str,
        *,
        kind: TalkerKind,
        arrived: float,
        spoken: bool = True,
        session: AgentSession | None = None,
        owner: tuple[str, int] | None = None,
        scope_authority: dict[str, tuple[int, int]] | None = None,
    ) -> TurnEnd:
        session = session or self._talker
        assert session is not None
        self._turn_count += 1
        turn = self._turn_count
        show = kind in ("user", "event")
        t0 = self._clock.now()
        shown, block = "", ""
        first_delta: float | None = None
        first_sentence: float | None = None
        tools: list[str] = []
        tool_before_text = False
        after_block = False
        end = TurnEnd("failed", error="the session ended without a result")
        self._talker_gate.turn = turn if show else None
        self._active_spoken_owner = owner
        self._active_scope_authority = scope_authority or {}
        self._active_scope_id = owner[0] if owner is not None else next(iter(self._active_scope_authority), None)
        self._turn_spoken = show and spoken
        self._turn_answers_user = self._turn_spoken and kind == "user"
        speech = self._speech if self._turn_spoken else _Silent()
        speech.begin(self._lang, turn=turn, heard_at=arrived if kind == "user" else None, owner=owner)
        self._replying = show
        try:
            async for event in self._guarded(session.send(message)):
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
                        self._limit.observe("talker", event)
        finally:
            self._talker_gate.turn = None
            self._active_spoken_owner = None
            for scope in self._scopes.settled(self._active_scope_authority):
                for task_id in scope.selected:
                    self._resume_scope_task(task_id, scope.task_context.get(task_id, ""))
            self._scopes.fail(self._active_scope_authority)
            self._active_scope_authority = {}
            self._active_scope_id = None
            self._turn_spoken = self._turn_answers_user = self._replying = False
            if self._interrupted_turn != turn:
                speech.end()
        if end.outcome == "interrupted" and self._interrupted_turn != turn:
            end = TurnEnd("failed", end.usage, "the turn stopped without the Hub interrupting it")
        if show:
            self._output.reply_end()
        # A failed restart has its own notice.
        if end.outcome == "failed" and kind != "restart":
            self._model_available = False
            self._output.notice(f"Model unavailable: the Talker failed: {end.error}. Try a new request; check Claude sign-in or connection if it persists.")
        if (
            end.outcome == "completed" and not self._model_available
            and not self._limit.active and not self._limit.recovery_pending
        ):
            self._model_available = True
            if self._on_model_ready is not None:
                self._on_model_ready()
        if show and (shown.strip() or end.outcome != "failed"):
            self._exchanges.append((message, shown))
        self._shown = shown

        def ms(t: float | None) -> int | None:
            return None if t is None else round((t - arrived) * 1000)

        self._record(
            self._talker_spec,
            kind,
            end,
            tools,
            turn=turn,
            request_at=arrived if kind == "user" else None,
            request_id=owner[0] if owner is not None else None,
            revision=owner[1] if owner is not None else None,
            started_at=t0,
            completed_at=self._clock.now(),
            time_basis="perf_counter",
            wait_ms=ms(t0),
            first_delta_ms=ms(first_delta),
            first_sentence_ms=ms(first_sentence),
            total_ms=ms(self._clock.now()),
            duration_ms=round((self._clock.now() - t0) * 1000),
            tool_before_text=tool_before_text,
            **({"context_tokens": end.context.tokens, "context_window": end.context.window} if end.context else {}),
            **({"no_context": end.no_context} if end.no_context else {}),
        )
        return end

    async def _orchestrate(self) -> None:
        if self._preconnect_orchestrator:
            try:
                self._orchestrator = await self._backend.open(self._orchestrator_spec)
            except Exception as e:
                self._output.notice(f"Background model unavailable: {type(e).__name__}: {e}. A new Task retries the connection.")
        while True:
            item = await self._orchestrator_inbox.get()
            if not item.restart and self._scopes.held(item.task_id):
                self._scope_inputs.append(item)
                if task := self._tasks.get(item.task_id):
                    task.discard_message()
                self._handled()
                continue
            try:
                await self._orchestrator_gate.before_next_turn()
                await self._orchestrator_turn(item)
            except Exception as e:
                # Whatever went wrong on this Task, the next one in the queue still gets its turn.
                error = f"{type(e).__name__}: {e}"
                task = self._tasks.get(item.task_id)
                if task is not None and task.state == "working" and task.revision == item.revision and not self._scopes.held(task.id):
                    self._fail(task, error)
                self._output.notice(f"The Orchestrator failed on {item.task_id}: {error}")
            finally:
                if not any(d is item for d in self._deferred):
                    answered = {id(item), *map(id, item.carried)}
                    self._unanswered = [i for i in self._unanswered if id(i) not in answered]
                self._handled()

    async def _orchestrator_turn(self, item: _OrchestratorInput) -> None:
        task = self._tasks.get(item.task_id)
        if task is not None and not item.restart:
            if not await self._scopes.wait(task.id) or task.revision != item.revision:
                task.discard_message()
                return
        if item.restart:
            tasks = [t for t in map(self._tasks.get, item.restart) if t is not None and not t.cancelled]
        else:
            tasks = [t for t in [self._tasks.get(item.task_id)] if t is not None]
        if not tasks:
            return
        if self._limit.blocks("orchestrator") and not all(task.cancelled for task in tasks):
            self._defer(item)
            return
        if self._orchestrator is None:
            try:
                self._orchestrator = await self._backend.open(self._orchestrator_spec)
            except Exception as e:
                failed = TurnEnd("failed", error=f"the Orchestrator could not start: {e}")
                for task in tasks:
                    if item.restart:
                        self._after_turn(task, failed, task.reports, item)
                    elif task.take_message():
                        self._fail(task, failed.error or "")
                return
        if not item.restart and tasks[0].revision != item.revision:
            tasks[0].discard_message()
            return
        # Taken after the last await before the turn, so a cancel lands either before it or on the running turn.
        if not item.restart and not tasks[0].take_message():
            return
        session = self._orchestrator
        self._turn_count += 1
        turn = self._turn_count
        reports = {task.id: task.reports for task in tasks}
        revisions = {task.id: task.revision for task in tasks}
        t0 = self._clock.now()
        tools: list[str] = []
        self._turn_tasks = tuple(task.id for task in tasks)
        self._turn_task_revisions = {task.id: task.revision for task in tasks}
        self._spawned = set()
        consuming = asyncio.create_task(self._consume(tasks, session, item.message, turn, tools))
        try:
            end = await self._within_time(consuming, session, turn)
        finally:
            self._turn_tasks = ()
            self._turn_task_revisions = {}
            self._orchestrator_gate.turn = None
            if not consuming.done():
                # A turn left mid-stream would feed its leftovers to the next message, so the session goes.
                consuming.cancel()
                await asyncio.wait([consuming])
                self._orchestrator = None
                with contextlib.suppress(Exception):
                    await session.close()
        self._limit.observe("orchestrator", end)
        if end.outcome == "limited":
            # The turn was not answered, so the same message is sent again once the limit resets.
            if not all(task.cancelled for task in tasks):
                if not item.restart:
                    tasks[0].queue_message()
                self._defer(item)
        else:
            for task in tasks:
                if task.revision == revisions[task.id]:
                    self._after_turn(task, end, reports[task.id], item)
        self._record(
            self._orchestrator_spec,
            "task",
            end,
            tools,
            task=",".join(task.id for task in tasks),
            task_revisions=revisions,
            wait_ms=round((t0 - item.arrived) * 1000),
            total_ms=round((self._clock.now() - item.arrived) * 1000),
            duration_ms=round((self._clock.now() - t0) * 1000),
        )

    def _after_turn(self, task: Task, end: TurnEnd, reports: int, item: _OrchestratorInput) -> None:
        if self._scopes.held(task.id) or (not item.restart and task.revision != item.revision):
            return
        # Only news still to come (a Subagent's or Watcher's end, a Check, a queued message) carries a Task on.
        quiet = not (task.id in self._spawned or task.waiting or self._has_pending(task.id))
        if item.restart and not (quiet and task.state == "working"):
            # After a restart only a Task nothing will wake needs this turn; one waiting on the User waits on.
            return
        if not task.cancelled and task.reports == reports:
            if end.outcome != "completed":
                self._fail(task, end.error or "the Orchestrator's turn was stopped")
            elif quiet and item.last_check:
                # Looking and finding nothing worth saying is a fair end for a Check, not a fault.
                task.settle()
                self._output.notice(f"{task.id}: its last Check came due and the Orchestrator had nothing to report.")
            elif quiet and item.restart:
                self._fail(task, "the Hub restarted and the Orchestrator did not take it up again")
            elif quiet:
                self._fail(task, "the Orchestrator ended its turn without a report")
        elif quiet:
            # A report while a wait was live left the Task working; the turn may have cancelled that wait since.
            task.settle()

    async def _consume(
        self, tasks: list[Task], session: AgentSession, message: str, turn: int, tools: list[str]
    ) -> TurnEnd:
        if all(task.cancelled for task in tasks):
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
        task.fail(reason, live=self._has_pending(task.id))
        text = f"{task.id} failed: {reason}."
        if self._subagents.live(task.id):
            text = f"The Orchestrator failed on {task.id}: {reason}. Its Subagents keep working and still report."
        self._post_news("hub", text, task.id)

    def _record(self, spec: SessionSpec, kind: str, end: TurnEnd, tools: list[str], **fields: Any) -> None:
        self._write(
            {
                "kind": kind,
                "role": spec.role,
                "model": spec.model,
                "effort": spec.effort,
                **fields,
                "tools": tools,
                "outcome": end.outcome,
                "usage": asdict(end.usage),
                **({"resets_at": end.resets_at} if end.resets_at is not None else {}),
            }
        )

    def _write(self, entry: dict[str, Any]) -> None:
        try:
            self._metrics.record(entry)
        except Exception as e:
            self._output.notice(f"Could not write metrics: {e}")

    def _subagent_turn(self, subagent: Subagent, spec: SessionSpec, end: TurnEnd, duration_ms: int) -> None:
        self._record(spec, "run", end, [], task=subagent.task_id, subagent=subagent.id, duration_ms=duration_ms)

    def _limited_reply(self, item: _TalkerInput) -> TurnEnd:
        """A held Talker needs no model call to show its availability."""
        speech = self._speech if item.spoken else _Silent()
        self._turn_count += 1
        speech.begin(self._lang, turn=self._turn_count, heard_at=item.arrived)
        self._limit_notice(self._limit.tell())
        self._persist()
        speech.end()
        self._output.reply_end()
        return TurnEnd("limited")

    def _limit_notice(self, resets_at: float | None, *, update: bool = False) -> None:
        when = update_sentence(resets_at, "en", self._wall()) if update else reset_sentence(resets_at, "en", self._wall())
        if resets_at is None:
            when = "Reset time unavailable."
        self._output.notice(
            f"Model unavailable: usage limit. {when} Held work retries automatically; try your request again after reset."
        )

    def _limit_hit(self, role: str, news: bool) -> None:
        self._write({"kind": "usage_limit", "role": role, "resets_at": self._limit.resets_at(role)})
        self._drop_reset_news()
        if role == "talker":
            self._model_available = False
        self._persist()
        if news:
            self._announce_limit_news()

    def _announce_limit_news(self) -> None:
        notice = self._limit.tell_news()
        if notice is not None:
            self._limit_notice(notice.resets_at, update=notice.update)
            self._persist()

    def _limit_cleared(self, roles: frozenset[str]) -> None:
        self._write({"kind": "usage_reset", "roles": sorted(roles)})
        if "orchestrator" in roles:
            for deferred in self._deferred:
                self._orchestrator_inbox.put_nowait(deferred)
            self._deferred.clear()
        if "talker" in roles:
            self._unpark()
        self._persist()

    def _limit_reset(self) -> None:
        """A turn of a held role completed after its hold cleared: the reset is real, so the User hears of it."""
        self._model_available = True
        self._output.notice("Model available: usage-limit recovery confirmed by a completed turn. Held work continues.")
        text = (
            "The usage limit has reset. Queued Subagents are starting and any waiting Task carries on. "
            "Say so in one sentence."
        )
        message = f'<event source="hub" reason="usage_reset">{text}</event>'
        # Ordinary news: batched into one Announcement with the rest, and held in Quiet mode.
        self._queue_news(Leaf(message, self._clock.now(), "The usage limit has reset.", reset=True))
        self._persist()

    def _park(self, item: _TalkerInput) -> int:
        """Keeps the news in `item` that the limited Talker could not tell, for after the reset; returns how many
        posted items that is. A User line keeps only the held news it opened with, as ordinary news. News of the
        reset is not kept: it was true when posted, and by the time the hold clears the next completed turn
        announces it afresh."""
        if item.news is None:
            return 0
        news = item.news if item.kind == "event" else News.of(item.news.leaves())
        untold = news.without_reset() if news else None
        if untold is None:
            return 0
        self._parked_news.append(untold)
        return untold.count

    def _unpark(self) -> None:
        """Puts parked news back, under the rules of news just posted; it is already counted as pending."""
        parked, self._parked_news = self._parked_news, []
        for news in parked:
            if self._floor is None:
                self._talker_inbox.put_nowait(_told(news))
            else:
                self._queue(news)
        self._arrived.set()

    def _defer(self, item: _OrchestratorInput) -> None:
        """Holds the Orchestrator's message until its limit resets; the Hub stays unsettled meanwhile."""
        self._deferred.append(item)
        self._pending += 1
        self._settled.clear()

    async def _resolve_scope(self, args: dict[str, Any]) -> str:
        choice = self._scopes.choose(args, self._active_scope_authority)
        if isinstance(choice, Refused):
            return choice.reason
        for task_id in choice.task_ids:
            task = self._tasks.get(task_id)
            if task is None or task.cancelled:
                return "Cannot revise an unknown or cancelled Task: " + task_id
        task_ids = set(choice.task_ids)
        prior_task_ids = set(choice.scope.task_ids)
        self._persist_suspended = True
        try:
            for task_id in task_ids:
                task = self._tasks.get(task_id)
                if task is not None:
                    task.revise(choice.scope.current_text)
            self._unanswered = [item for item in self._unanswered if item.task_id not in task_ids]
            for task_id in task_ids:
                self._approvals.withdraw(task_id, reason="Its spoken request was revised.")
                self._waits.cancel_task(task_id)
                for subagent in self._subagents.live(task_id):
                    self._subagents.cancel(subagent.id)
            dropped = len(self._held)
            self._held = [leaf for leaf in self._held if leaf.task_id not in task_ids]
            dropped -= len(self._held)
            def keep(items: Iterable[News]) -> list[News]:
                nonlocal dropped
                remaining: list[News] = []
                for news in items:
                    rest = news.without_tasks(task_ids)
                    dropped += news.count - (rest.count if rest else 0)
                    if rest:
                        remaining.append(rest)
                return remaining
            self._news = deque(keep(self._news))
            self._parked_news = keep(self._parked_news)
            for _ in range(dropped):
                self._handled()
            for task_id in task_ids:
                self._needs_user.pop(task_id, None)
            self._scopes.commit(choice)
        finally:
            self._persist_suspended = False
        self._persist()
        if self._active_spoken_owner is None:
            self._active_scope_id = choice.scope.id
        if self._floor is not None:
            self._floor.semantic_context((choice.scope.id, choice.scope.revision), choice.scope.semantic_start)
        self._release_scope_news()
        self._release_scope_work()
        for approval in self._approvals.live():
            if approval.task_id in prior_task_ids and approval.state == "approved" and approval.action in HUB_ACTIONS and not self._scopes.held(approval.task_id) and approval.id not in self._approval_runs:
                self._pending += 1
                self._settled.clear()
                self._carry_out(approval)
        for task_id in prior_task_ids:
            if task_id not in task_ids:
                self._resume_scope_task(task_id, choice.scope.task_context.get(task_id, ""))
        turn = self._orchestrator_gate.turn
        if self._turn_tasks and set(self._turn_tasks).issubset(task_ids) and turn is not None and self._orchestrator is not None:
            await self._orchestrator_gate.interrupt(self._orchestrator, turn)
        return "Resolved spoken scope: " + choice.disposition + "."

    def _resume_scope_task(self, task_id: str, previous: str = "") -> None:
        task = self._tasks.get(task_id)
        active = task_id in self._turn_tasks and self._task_current(task_id)
        if task is None or task.state not in ("working", "queued") or active or task.waiting or self._scopes.held(task_id) or self._has_pending(task_id):
            return
        context = "Task scope was explicitly resolved. Continue existing work without repeating completed actions."
        if previous:
            context += "\nPrevious Task context: " + previous
        context += "\n" + "\n".join(self._task_lines(task_id))
        task.queue_message()
        self._ask(_OrchestratorInput(task.id, task_message(task, task.request, context, continues=True),
                                    self._clock.now(), revision=task.revision))

    def _release_scope_work(self) -> None:
        waiting, self._scope_inputs = self._scope_inputs, []
        released: set[str] = set()
        for item in waiting:
            task = self._tasks.get(item.task_id)
            if task is None or task.cancelled or task.revision != item.revision:
                self._unanswered = [old for old in self._unanswered if old is not item]
                continue
            if self._scopes.held(task.id):
                self._scope_inputs.append(item)
                continue
            task.queue_message()
            self._post(self._orchestrator_inbox, item)
            released.add(task.id)
        for subagent_id, subagent in list(self._scope_queued.items()):
            if self._scopes.held(subagent.task_id):
                continue
            self._scope_queued.pop(subagent_id)
            task = self._tasks.get(subagent.task_id)
            if task is None or task.cancelled or task.revision != subagent.task_revision:
                subagent.stop_requested = True
                subagent.stop.set()
            self._pending += 1
            self._settled.clear()
            self._subagents.start(subagent)
            released.add(subagent.task_id)
        resume_waits: set[str] = set()
        for wait_id in list(self._scope_waits):
            wait = self._waits.get(wait_id)
            if wait is None or wait.state != "waiting":
                self._scope_waits.discard(wait_id)
            elif not self._scopes.held(wait.task_id):
                self._scope_waits.discard(wait_id)
                resume_waits.add(wait_id)
                released.add(wait.task_id)
                if isinstance(wait, Watcher):
                    self._pending += 1
                    self._settled.clear()
        self._waits.resume(only=resume_waits)
        for scope in self._scopes.pending():
            released.difference_update(scope.task_ids)
        for task_id in released:
            self._resume_scope_task(task_id)
        self._persist()

    def _task_current(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        return task is not None and self._turn_task_revisions.get(task_id) == task.revision

    def _task_tool(self, handler: ToolHandler) -> ToolHandler:
        async def guarded(args: dict[str, Any]) -> str:
            target = args.get("task_id")
            if target is None and args.get("subagent_id"):
                subagent = self._subagents.get(args["subagent_id"])
                target = subagent.task_id if subagent is not None else None
            if target is None and args.get("wait_id"):
                wait = self._waits.get(args["wait_id"])
                target = wait.task_id if wait is not None else None
            if target is None and len(self._turn_tasks) == 1:
                target = self._turn_tasks[0]
            if isinstance(target, str):
                source_revisions = {target: self._turn_task_revisions.get(target)} if target in self._turn_tasks else dict(self._turn_task_revisions)
                task = self._tasks.get(target)
                target_revision = task.revision if task is not None else None
                if not await self._scopes.wait(target):
                    return "Refused: Task scope remains unresolved."
                task = self._tasks.get(target)
                if (task is not None and task.revision != target_revision) or any(
                    (source := self._tasks.get(task_id)) is None or source.revision != revision
                    for task_id, revision in source_revisions.items()
                ):
                    return "Refused: this Task revision was superseded."
            return await handler(args)
        return guarded

    def _owned_tool(self, handler: ToolHandler) -> ToolHandler:
        async def guarded(args: dict[str, Any]) -> str:
            if self._closing or self._interrupted_turn == self._talker_gate.turn and self._talker_gate.turn is not None:
                return "Refused: this spoken generation was superseded."
            owner = self._active_spoken_owner
            if owner is not None and self._floor is not None and not self._floor.owns(owner):
                return "Refused: this spoken request revision is no longer current."
            return await handler(args)
        return guarded

    async def _delegate(self, args: dict[str, Any]) -> str:
        request = str(args.get("request") or "").strip()
        context = str(args.get("context") or "").strip()
        if not request:
            return "request is required."
        scope = self._scopes.get(self._active_scope_id)
        if scope is not None and scope.pending:
            return "Resolve the pending spoken scope before delegating work; choose its complete affected Task set."
        if args.get("new_task") and args.get("task_id"):
            return "Choose task_id for existing work or new_task=true for additional work, never both."
        if scope is not None and scope.revision > 0 and scope.task_ids:
            known = set(scope.task_ids)
            if args.get("task_id") and args["task_id"] not in known:
                return "Choose task_id from this spoken request's Tasks: " + ", ".join(sorted(known))
            if args.get("task_id") and scope.disposition == "revise_tasks" and args["task_id"] not in scope.selected:
                return "This scope did not select that Task for revision."
            if not args.get("new_task") and not args.get("task_id"):
                return "Choose task_id for selected existing work, or new_task=true for additional work."
        continues = bool(args.get("task_id"))
        if continues:
            task = self._tasks.get(args["task_id"])
            if task is None:
                return f"{no_task(args['task_id'])} Delegate without task_id to start a new one."
            if task.cancelled:
                return f"{task.id} was cancelled. Delegate without task_id to start a new one."
        else:
            task = self._tasks.create(request, self._clock.now())
        self._scopes.bind(self._active_scope_id, task.id, task.request)
        words = scope.current_text if scope is not None else self._user_text
        if words is not None and (not task.words or task.words[-1] != words):
            task.words.append(words)
        task.queue_message()
        message = task_message(task, request, context, continues=continues)
        self._ask(_OrchestratorInput(task.id, message, self._clock.now(), revision=task.revision))
        return json.dumps({"task_id": task.id, "queued": True})

    async def _status(self, args: dict[str, Any]) -> str:
        return self._tasks.status(self._clock.now(), self._task_lines)

    def _task_lines(self, task_id: str) -> list[str]:
        deferred = any(task_id == d.task_id or task_id in d.restart for d in self._deferred)
        held = ["waiting for the usage limit to reset"] if deferred else []
        task = self._tasks.get(task_id)
        revision = task.revision if task is not None else None
        return held + self._approvals.lines(task_id) + self._subagents.lines(task_id, revision=revision) + self._waits.lines(task_id, revision=revision)

    def _has_pending(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None:
            return False
        return bool(any(subagent.task_revision == task.revision and not subagent.stop_requested
                        for subagent in self._subagents.live(task_id))
                    or any(wait.task_revision == task.revision for wait in self._waits.live(task_id))
                    or any(item.task_id == task_id and item.revision == task.revision for item in self._scope_inputs))

    async def _cancel(self, args: dict[str, Any]) -> str:
        if wait := self._waits.get(args.get("task_id")):
            if self._scopes.held(wait.task_id):
                return "Resolve the pending spoken scope before cancelling this wait."
            return self._cancel_wait_for_user(args["task_id"])
        task = self._tasks.get(args.get("task_id"))
        if task is None:
            return no_task(args.get("task_id"))
        if self._scopes.held(task.id):
            return "Resolve the pending spoken scope before cancelling this Task."
        if not task.cancel():
            return f"{task.id} is already {task.state}."
        for subagent in self._subagents.live(task.id):
            self._subagents.cancel(subagent.id)
        self._waits.cancel_task(task.id)
        self._approvals.withdraw(task.id)
        self._persist()
        turn = self._orchestrator_gate.turn
        # The recovery turn is on every open Task, so cancelling one of them leaves it running.
        if self._turn_tasks == (task.id,) and turn is not None and self._orchestrator is not None:
            await self._orchestrator_gate.interrupt(self._orchestrator, turn)
        return f"Cancelled {task.id}."

    async def _show(self, args: dict[str, Any]) -> str:
        markdown = str(args.get("markdown") or "")
        if not markdown.strip():
            return "markdown is required."
        self._output.show(markdown)
        return "Shown on screen."

    async def _end_conversation(self, args: dict[str, Any]) -> str:
        # Text mode, a typed line in voice mode, or news has no listening of its own to stop: only acknowledged.
        if self._on_end_conversation is not None and self._turn_answers_user:
            self._on_end_conversation()
        return "The conversation ends after this reply."

    async def _quiet_mode_tool(self, args: dict[str, Any]) -> str:
        on = args.get("on") in (True, "true")
        if self._floor is None:
            return "Quiet mode is for voice mode; here news only appears on screen, so nothing changed."
        if not on:
            self._release_held()
            return "Quiet mode is off."
        self._quiet_mode = True
        # News already waiting for this reply to end waits for the Wake word instead, all but what gets through.
        waiting, self._news = self._news, deque()
        for news in waiting:
            self._queue(news)
        self._persist()
        return "Quiet mode is on until the User's next Wake word. News that needs the User still comes through."

    async def _report(self, args: dict[str, Any]) -> str:
        task = self._tasks.get(args.get("task_id"))
        if task is None:
            return no_task(args.get("task_id"))
        if task.id not in self._turn_tasks:
            on = ", ".join(self._turn_tasks)
            return f"You are working on {on}: report on {on} only."
        if task.cancelled:
            return f"{task.id} was cancelled; the User will not hear this."
        spoken = str(args.get("spoken") or "").strip()
        if not spoken:
            return "spoken is required."
        details = str(args.get("details_markdown") or "").strip() or None
        needs_user = args.get("needs_user") in (True, "true")
        approval: Approval | None = None
        if args.get("approval"):
            requested = await self._approvals.request(task.id, args["approval"], task_revision=self._turn_task_revisions[task.id])
            if not self._task_current(task.id):
                self._approvals.withdraw(task.id, reason="Its spoken request was revised.")
                return "Refused: this Task revision was superseded."
            if isinstance(requested, Refused):
                return requested.reason
            approval, needs_user = requested, True
            # The screen names the exact action in the Hub's words, whatever the sentence says.
            exact = f"**Approval {approval.id}:** {approval.text}"
            details = f"{exact}\n\n{details}" if details else exact
        task.report(spoken, needs_user=needs_user, live=self._has_pending(task.id))
        news = spoken
        if approval is not None:
            news += f" Ask exactly one Approval question, in the User's language: {approval.question()} Italian: {approval.question('it')}"
        self._post_news(
            "orchestrator", news, task.id, needs_user=needs_user, details=details, approval=approval and approval.id
        )
        if approval is not None:
            return f"Reported to the User, asking for Approval {approval.id}: {approval.text}."
        return "Reported to the User."

    async def _approve(self, args: dict[str, Any]) -> str:
        wanted = self._approvals.get(args.get("approval_id"))
        if (wanted is not None and self._scopes.held(wanted.task_id)) or (
            wanted is None and any(self._scopes.held(approval.task_id) for approval in self._approvals.pending())
        ):
            return "Resolve the pending spoken scope before using an old Approval."
        decided = self._approvals.decide(args.get("approval_id"))
        if isinstance(decided, str):
            return decided
        task = self._tasks.get(decided.task_id)
        self._scopes.bind(self._active_scope_id, decided.task_id, task.request if task is not None else "")
        self._persist()
        if decided.action in HUB_ACTIONS:
            self._pending += 1
            self._settled.clear()
            self._carry_out(decided)
            return (
                f"Approved {decided.id} with the User's words. The action is running now ({decided.text}), and "
                "the result comes as news: say so in a few words."
            )
        text = (
            f"The User approved {decided.id} ({decided.text}) in these words: {decided.words!r}. Start the Subagent "
            f"that carries it out with approval_id {decided.id}; its brief then carries the Approval."
        )
        self._wake(decided.task_id, approval_event(decided, "approved", text))
        return f"Approved {decided.id} with the User's words; the work is starting. Describe it as your own work in a few words."

    def _carry_out(self, approval: Approval) -> None:
        if approval.id in self._approval_runs:
            self._handled()
            return
        async def authorize() -> Refused | None:
            if not await self._scopes.wait(approval.task_id):
                return Refused("Task scope remains unresolved; the action has not been dispatched.")
            task = self._tasks.get(approval.task_id)
            if self._closing or task is None or task.cancelled or task.revision != approval.task_revision:
                approval.state, approval.outcome = "withdrawn", "Its Task revision was superseded before dispatch."
                self._persist()
                return Refused(approval.outcome)
            return None
        async def run() -> None:
            try:
                event = await self._approvals.carry_out(approval, authorize=authorize,
                    authority_version=lambda: self._scopes.version(approval.task_id))
                task = self._tasks.get(approval.task_id)
                if task is not None and task.revision == approval.task_revision:
                    self._wake(approval.task_id, event)
            except Exception as error:
                self._output.notice(f"The Hub could not carry out {approval.id}: {type(error).__name__}: {error}")
            finally:
                self._handled()
        carrying = asyncio.create_task(run())
        self._approval_runs[approval.id] = carrying
        self._carrying_out.add(carrying)
        def finished(task: asyncio.Task[None]) -> None:
            self._carrying_out.discard(task)
            if self._approval_runs.get(approval.id) is task:
                self._approval_runs.pop(approval.id)
        carrying.add_done_callback(finished)

    def _subagent_session(self, subagent: Subagent) -> SessionSpec:
        spec = self._session_for(subagent)
        guard = spec.mcp_guard
        if subagent.approval is not None:
            guard = self._approvals.guard(subagent.approval, subagent.id, guard or connector_check)
        async def lease(name: str, args: Mapping[str, Any]) -> Refused | None:
            if spec.before_tool is not None:
                refused = await spec.before_tool(name, args)
                if refused is not None:
                    return refused
            if not await self._scopes.wait(subagent.task_id):
                return Refused("Task scope remains unresolved.")
            task = self._tasks.get(subagent.task_id)
            if self._closing or task is None or task.cancelled or task.revision != subagent.task_revision:
                return Refused("This Subagent's Task revision was superseded.")
            return None
        return replace(spec, mcp_guard=guard, before_tool=lease)

    def _turn_task(self, tool: str, args: dict[str, Any]) -> Task | str:
        """The Task a tool call is for: the turn's own, or in the recovery turn the one its task_id names."""
        on = list(self._turn_tasks)
        if not on:
            return f"{tool} works only while you are on a Task."
        wanted = args.get("task_id")
        if not wanted and len(on) > 1:
            return f"task_id is required in this turn: one of {', '.join(on)}."
        task = self._tasks.get(wanted or on[0])
        if task is None or task.id not in on:
            return f"You are working on {', '.join(on)}: {tool} for those only."
        return task

    async def _spawn_subagent(self, args: dict[str, Any]) -> str:
        task = self._turn_task("spawn_subagent", args)
        if isinstance(task, str):
            return task
        approval: Approval | None = None
        if args.get("approval_id"):
            granted = self._approvals.grant(args["approval_id"], task.id)
            if isinstance(granted, str):
                return granted
            approval = granted
        prepared = await self._subagents.prepare(task.id, args, task.words, task_revision=self._turn_task_revisions[task.id])
        if isinstance(prepared, Refused):
            return prepared.reason
        if not await self._scopes.wait(task.id) or not self._task_current(task.id):
            prepared.stop_requested = True
            prepared.stop.set()
            self._producer_revisions[prepared.id] = self._turn_task_revisions.get(task.id, task.revision - 1)
            self._pending += 1
            self._settled.clear()
            self._subagents.start(prepared)
            return "Refused: this Task revision was superseded during preparation; its unused worktree is released."
        if approval is not None and approval.state == "approved":
            prepared.approval = approval.id
            prepared.brief += approval_brief(approval)
            self._approvals.hand_to(approval, prepared.id)
        self._spawned.add(task.id)
        self._producer_revisions[prepared.id] = task.revision
        # A running Subagent keeps the Hub from settling, so text mode waits for its report.
        self._pending += 1
        self._settled.clear()
        state = self._subagents.start(prepared)
        # The Approval may have lapsed while the worktree was made: a Subagent started for it must not run without it.
        if task.cancelled or (approval is not None and prepared.approval is None):
            self._subagents.cancel(prepared.id)
        self._persist()
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
        self._approvals.subagent_ended(subagent.id)
        try:
            task = self._tasks.get(subagent.task_id)
            if task is None or not self._producer_current(subagent.id, subagent.task_id):
                return
            if self._closing:
                # Nobody hears of it now: the next launch's recovery message tells the Orchestrator.
                if subagent.state == "interrupted" and not task.cancelled:
                    self._interrupted.append(subagent.id)
                return
            if not task.cancelled:
                self._wake(task.id, ended_event(subagent))
            elif subagent.writes:
                # The Orchestrator hears nothing more of a cancelled Task, so the Hub says what its branch keeps.
                self._post_news("hub", kept_line(subagent), task.id, details=subagent.report)
        finally:
            self._handled()

    def _producer_current(self, producer_id: str, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        return task is not None and self._producer_revisions.get(producer_id, 0) == task.revision

    def _subagent_over_time(self, subagent: Subagent) -> None:
        if not self._producer_current(subagent.id, subagent.task_id):
            return
        self._wake(subagent.task_id, over_time_event(subagent, self._clock.now()))

    def _wake(self, task_id: str, message: str, *, last_check: bool = False) -> None:
        """Queue news for the Orchestrator on a Task; a cancelled Task hears nothing more."""
        task = self._tasks.get(task_id)
        if task is None or task.cancelled:
            return
        task.queue_message()
        self._ask(_OrchestratorInput(task.id, message, self._clock.now(), last_check, revision=task.revision))

    async def _watch(self, args: dict[str, Any]) -> str:
        task = self._turn_task("watch", args)
        if isinstance(task, str) or task.cancelled:
            return task if isinstance(task, str) else "watch works only while you are on a Task."
        watcher = self._waits.watch(task.id, str(args.get("kind") or ""), str(args.get("target") or ""))
        if isinstance(watcher, Refused):
            return watcher.reason
        watcher.task_revision = task.revision
        self._producer_revisions[watcher.id] = task.revision
        # A Watcher ends on its own, so text mode waits for it like a Subagent.
        self._pending += 1
        self._settled.clear()
        self._persist()
        return json.dumps(
            {
                "watcher_id": watcher.id,
                "watching": " ".join(watcher.command.watch),
                "note": "Its end reaches you as a watcher event. End this turn without a report unless the User "
                "should hear something now.",
            }
        )

    async def _schedule_check(self, args: dict[str, Any]) -> str:
        task = self._turn_task("schedule_check", args)
        if isinstance(task, str) or task.cancelled:
            return task if isinstance(task, str) else "schedule_check works only while you are on a Task."
        note = str(args.get("note") or "")
        check = self._waits.schedule(task.id, args.get("after_seconds"), note, args.get("max_repeats"))
        if isinstance(check, Refused):
            return check.reason
        check.task_revision = task.revision
        self._producer_revisions[check.id] = task.revision
        self._persist()
        return json.dumps(
            {
                "check_id": check.id,
                "first_in_seconds": round(check.interval),
                "max_repeats": check.max_repeats,
                "note": "Each time it comes due you get a timer event; each wait is twice the one before. "
                "cancel_wait stops it once you no longer need it.",
            }
        )

    async def _cancel_wait(self, args: dict[str, Any]) -> str:
        if not self._turn_tasks:
            return "cancel_wait works only while you are on a Task."
        wait = self._waits.get(args.get("wait_id"))
        on = wait.task_id if wait is not None and wait.task_id in self._turn_tasks else self._turn_tasks[0]
        cancelled = self._waits.cancel(args.get("wait_id"), task_id=on)
        if isinstance(cancelled, str):
            return cancelled
        self._persist()
        return f"Cancelled {cancelled.id}."

    def _cancel_wait_for_user(self, wait_id: str) -> str:
        """The User stops one wait: its Task goes on, and the Orchestrator hears of it to decide what is next."""
        cancelled = self._waits.cancel(wait_id)
        if isinstance(cancelled, str):
            return cancelled
        self._wake(cancelled.task_id, cancelled_event(cancelled))
        self._persist()
        return f"Cancelled {cancelled.id}."

    def _watcher_ended(self, watcher: Watcher) -> None:
        unstarted = watcher.id in self._scope_waits
        self._scope_waits.discard(watcher.id)
        try:
            if watcher.state != "cancelled" and self._producer_current(watcher.id, watcher.task_id):
                self._wake(watcher.task_id, watcher_event(watcher))
        finally:
            if not unstarted:
                self._handled()

    def _check_due(self, check: Check) -> None:
        if not self._producer_current(check.id, check.task_id):
            return
        self._wake(check.task_id, check_event(check), last_check=check.state == "done")
