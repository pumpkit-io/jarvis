"""The Hub core: turns User input into Talker turns, hands Tasks to the Orchestrator, and relays its reports.

Every outside dependency is injected (agent backend, clock, output, metrics, speech) so tests and text mode
share this one code path.
"""

import asyncio
import contextlib
import json
import re
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass, replace
from html import escape
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
    TurnEnd,
)
from jarvis_hub.config import DEFAULT_TALKER_RESTART_AT
from jarvis_hub.orchestrator import orchestrator_tools
from jarvis_hub.subagents import Subagent, SubagentSetup, Subagents, ended_event, kept_line, over_time_event
from jarvis_hub.talker import talker_tools
from jarvis_hub.tasks import Task, Tasks, no_task
from jarvis_hub.voice.language import Lang, detect_language
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

    def begin(self, lang: Lang, *, turn: int, heard_at: float | None) -> None: ...

    def feed(self, text: str) -> None: ...

    def block_end(self) -> None: ...

    def end(self) -> None: ...

    def stop(self) -> None: ...


class Floor(Protocol):
    """Voice mode's say over when the Talker may take a turn the User did not ask for (`VoiceMode`)."""

    async def quiet(self) -> None:
        """Returns once Jarvis is idle with nothing playing."""
        ...

    def announce(self) -> bool:
        """Claims the voice for news if Jarvis is still idle, so a Wake word interrupts it; False if not idle."""
        ...


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
    """A line the User typed or said, stamped when it was read or the User stopped speaking, not when the Hub got to it."""

    text: str
    arrived: float
    # Left out, the Hub detects it from the text.
    lang: Lang | None = None
    typed: bool = True


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


def event_message(source: str, text: str, task: str, *, needs_user: bool = False, details: bool = False) -> str:
    on_screen = ' details="on screen"' if details else ""
    flag = "true" if needs_user else "false"
    return f'<event source="{source}" task="{task}" needs_user="{flag}"{on_screen}>{escape(text, quote=False)}</event>'


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


def away_message(count: int) -> str:
    updates = "update" if count == 1 else "updates"
    return f'<event source="hub" away="{count}">{count} {updates} while you were away; their details are on screen.</event>'


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
    message: str
    kind: TalkerKind
    arrived: float
    details: str | None = None
    heard: str | None = None
    lang: Lang | None = None
    typed: bool = True
    spoken: bool = True
    # How many posted items this one carries: news is merged into one turn, and each item counts as handled.
    count: int = 1
    # News only: its line in the "while you were away" list, and whether it gets through Quiet mode.
    news: str | None = None
    urgent: bool = False
    # The "while you were away" item, and the news it holds.
    away: bool = False
    parts: tuple["_TalkerInput", ...] = ()


def _merged(items: "list[_TalkerInput]") -> _TalkerInput:
    """Items as one turn, in order: the first item's kind and arrival, every message, every detail."""
    details = "\n\n".join(item.details for item in items if item.details)
    return replace(
        items[0],
        message="\n".join(item.message for item in items),
        details=details or None,
        count=sum(item.count for item in items),
    )


@dataclass(frozen=True)
class _OrchestratorInput:
    task_id: str
    message: str
    arrived: float
    # The message is a Check's last firing: no later event will wake the Orchestrator for it.
    last_check: bool = False


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
        waits: WaitSetup,
        orchestrator_timeout: float = ORCHESTRATOR_TIMEOUT,
        speech: Speech | None = None,
        speak_typed: bool = True,
        on_end_conversation: Callable[[], None] | None = None,
        talker_restart_at: float = DEFAULT_TALKER_RESTART_AT,
        floor: Floor | None = None,
    ) -> None:
        tools = talker_tools(
            delegate=self._delegate,
            status=self._status,
            cancel=self._cancel,
            show=self._show,
            end_conversation=self._end_conversation,
            quiet_mode=self._quiet_mode_tool,
        )
        self._talker_spec = replace(talker, tools=tools)
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
        self._backend = backend
        self._clock = clock
        self._output = output
        self._metrics = metrics
        self._orchestrator_timeout = orchestrator_timeout
        self._speech: Speech = speech if speech is not None else _Silent()
        # In voice mode a typed line gets a text-only reply; with `--text --speak` it is spoken.
        self._speak_typed = speak_typed
        self._on_end_conversation = on_end_conversation
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
        self._news: deque[_TalkerInput] = deque()
        # Quiet mode, and the news it holds for the next Wake word.
        self._quiet_mode = False
        self._held: list[_TalkerInput] = []
        # Per Task, the needs_user news last let through: the same again waits out Quiet mode.
        self._needs_user: dict[str, str] = {}
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
        self._talker_gate = _TurnGate()
        # Whether the Talker turn under way is heard: only a spoken turn's reply is interrupted.
        self._turn_spoken = False
        # Only the reply to the User's own spoken turn ends the conversation; news never does.
        self._turn_answers_user = False
        self._interrupted_turn: int | None = None
        self._orchestrator_gate = _TurnGate()
        self._orchestrating: str | None = None
        # Subagents the running Orchestrator turn started: their completion events bring the Task's next turn.
        self._spawned = 0
        # The User's own text in the Talker turn under way, for the Tasks it delegates.
        self._user_text: str | None = None
        self._subagents = Subagents(
            setup=subagents,
            backend=backend,
            clock=clock,
            on_end=self._subagent_ended,
            on_over_time=self._subagent_over_time,
        )
        self._waits = Waits(setup=waits, clock=clock, on_watcher_end=self._watcher_ended, on_check=self._check_due)

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
        self._input_ended.set()
        self._release_held()
        settling = asyncio.create_task(self._settled.wait())
        await asyncio.wait([settling, talking], return_when=asyncio.FIRST_COMPLETED)
        settling.cancel()

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
        for check in await self._waits.close():
            self._output.notice(f"{check.id} dropped; Checks aren't kept across restarts yet.")
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
                message = user_message(text, lang, typed=line.typed)
                spoken = not line.typed or self._speak_typed
                item = _TalkerInput(message, "user", line.arrived, heard=text, lang=lang, typed=line.typed, spoken=spoken)
                self._post(self._talker_inbox, item)

    def _post(self, inbox: "asyncio.Queue[Any]", item: _TalkerInput | _OrchestratorInput) -> None:
        """Queue an item; the Hub counts as settled again once every posted item is handled."""
        self._pending += 1
        self._settled.clear()
        inbox.put_nowait(item)
        self._arrived.set()

    def _post_news(self, source: str, text: str, task: str, *, needs_user: bool = False, details: str | None = None) -> None:
        message = event_message(source, text, task, needs_user=needs_user, details=details is not None)
        item = _TalkerInput(message, "event", self._clock.now(), details, news=f"{task}: {text}")
        if self._floor is None:
            self._post(self._talker_inbox, item)
            return
        key = _needs_user_key(text)
        urgent = needs_user and self._needs_user.get(task) != key
        if needs_user:
            self._needs_user[task] = key
        else:
            self._needs_user.pop(task, None)
        self._pending += 1
        self._settled.clear()
        if self._quiet_mode and not urgent:
            self._held.append(item)
        else:
            self._news.append(replace(item, urgent=urgent))
        self._arrived.set()

    def woke(self) -> None:
        """A Wake word opened listening: it ends Quiet mode, and the news it held opens the User's next turn."""
        self._release_held()

    def _release_held(self) -> None:
        """Ends Quiet mode. What it held goes first in the news, as one "while you were away" item."""
        self._quiet_mode = False
        if not self._held:
            return
        held, self._held = self._held, []
        listed = "\n".join(f"- {item.news}" for item in held)
        details = "\n\n".join([f"**While you were away**\n\n{listed}", *(item.details for item in held if item.details)])
        message = "\n".join([away_message(len(held)), *(item.message for item in held)])
        away = _TalkerInput(message, "event", self._clock.now(), details, count=len(held), away=True, parts=tuple(held))
        self._news.appendleft(away)
        self._arrived.set()

    def _with_away(self, item: _TalkerInput) -> _TalkerInput:
        """The User's first spoken turn after the Wake word that ended Quiet mode opens with what it held."""
        if item.kind != "user" or item.typed or not self._news:
            return item
        away = self._news[0]
        if not away.away or away.arrived > item.arrived:
            return item
        self._news.popleft()
        return replace(
            item, message=f"{away.message}\n{item.message}", details=away.details, count=item.count + away.count
        )

    def _handled(self) -> None:
        self._pending -= 1
        if self._pending == 0:
            self._settled.set()

    async def _talk(self) -> None:
        try:
            self._talker = await self._open_talker()
            await self._turn(WARM_UP, kind="warm_up", arrived=self._clock.now())
        except Exception as e:
            self._output.notice(f"The Talker could not start: {e}")
            return
        upcoming: _TalkerInput | None = None
        while True:
            if upcoming is None:
                item = await self._next_input()
            else:
                item, upcoming = upcoming, None
                await self._talker_gate.before_next_turn()
            item = self._with_away(item)
            try:
                if item.lang:
                    self._lang = item.lang
                if item.heard:
                    self._output.heard(item.heard, typed=item.typed)
                if item.details:
                    self._output.show(item.details)
                self._user_text = item.heard if item.kind == "user" else None
                end = await self._turn(item.message, kind=item.kind, arrived=item.arrived, spoken=item.spoken)
            finally:
                self._user_text = None
                for _ in range(item.count):
                    self._handled()
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
                return replace(self._news.popleft(), spoken=False)
            if self._news and quiet and self._floor.announce():
                # Everything waiting goes as one Announcement rather than one Follow-up window each.
                news = list(self._news)
                self._news.clear()
                return _merged(news)
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
        self._turn_spoken = show and spoken
        self._turn_answers_user = self._turn_spoken and kind == "user"
        speech = self._speech if self._turn_spoken else _Silent()
        speech.begin(self._lang, turn=turn, heard_at=arrived if kind == "user" else None)
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
        finally:
            self._talker_gate.turn = None
            self._turn_spoken = self._turn_answers_user = False
            if self._interrupted_turn != turn:
                speech.end()
        if end.outcome == "interrupted" and self._interrupted_turn != turn:
            end = TurnEnd("failed", end.usage, "the turn stopped without the Hub interrupting it")
        if show:
            self._output.reply_end()
        # A failed restart has its own notice.
        if end.outcome == "failed" and kind != "restart":
            self._output.notice(f"The Talker failed: {end.error}")
        if show and (shown.strip() or end.outcome != "failed"):
            self._exchanges.append((message, shown))

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
            **({"context_tokens": end.context.tokens, "context_window": end.context.window} if end.context else {}),
            **({"no_context": end.no_context} if end.no_context else {}),
        )
        return end

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
        self._spawned = 0
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
        # Only news still to come (a Subagent's or Watcher's end, a Check, a queued message) carries a Task on.
        quiet = not (self._spawned or task.waiting or self._has_pending(task.id))
        if not task.cancelled and task.reports == reports:
            if end.outcome != "completed":
                self._fail(task, end.error or "the Orchestrator's turn was stopped")
            elif quiet and item.last_check:
                # Looking and finding nothing worth saying is a fair end for a Check, not a fault.
                task.settle()
                self._output.notice(f"{task.id}: its last Check came due and the Orchestrator had nothing to report.")
            elif quiet:
                self._fail(task, "the Orchestrator ended its turn without a report")
        elif quiet:
            # A report while a wait was live left the Task working; the turn may have cancelled that wait since.
            task.settle()
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
        task.fail(reason, live=self._has_pending(task.id))
        text = f"{task.id} failed: {reason}."
        if self._subagents.live(task.id):
            text = f"The Orchestrator failed on {task.id}: {reason}. Its Subagents keep working and still report."
        self._post_news("hub", text, task.id)

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
        else:
            task = self._tasks.create(request, self._clock.now())
        if self._user_text is not None:
            task.words.append(self._user_text)
        task.queue_message()
        message = task_message(task, request, context, continues=continues)
        self._post(self._orchestrator_inbox, _OrchestratorInput(task.id, message, self._clock.now()))
        return json.dumps({"task_id": task.id, "queued": True})

    async def _status(self, args: dict[str, Any]) -> str:
        return self._tasks.status(self._clock.now(), self._task_lines)

    def _task_lines(self, task_id: str) -> list[str]:
        return self._subagents.lines(task_id) + self._waits.lines(task_id)

    def _has_pending(self, task_id: str) -> bool:
        return bool(self._subagents.live(task_id) or self._waits.live(task_id))

    async def _cancel(self, args: dict[str, Any]) -> str:
        if self._waits.get(args.get("task_id")) is not None:
            return self._cancel_wait_for_user(args["task_id"])
        task = self._tasks.get(args.get("task_id"))
        if task is None:
            return no_task(args.get("task_id"))
        if not task.cancel():
            return f"{task.id} is already {task.state}."
        for subagent in self._subagents.live(task.id):
            self._subagents.cancel(subagent.id)
        self._waits.cancel_task(task.id)
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
        # News already waiting for this reply to end waits for the Wake word instead.
        waiting = [item for item in self._news if not item.urgent]
        self._news = deque(item for item in self._news if item.urgent)
        self._held += [part for item in waiting for part in (item.parts or (item,))]
        return "Quiet mode is on until the User's next Wake word. News that needs the User still comes through."

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
        task.report(spoken, needs_user=needs_user, live=self._has_pending(task.id))
        self._post_news("orchestrator", spoken, task.id, needs_user=needs_user, details=details)
        return "Reported to the User."

    async def _spawn_subagent(self, args: dict[str, Any]) -> str:
        task = self._tasks.get(self._orchestrating)
        if task is None:
            return "spawn_subagent works only while you are on a Task."
        prepared = await self._subagents.prepare(task.id, args, task.words)
        if isinstance(prepared, Refused):
            return prepared.reason
        self._spawned += 1
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
            if not task.cancelled:
                self._wake(task.id, ended_event(subagent))
            elif subagent.writes:
                # The Orchestrator hears nothing more of a cancelled Task, so the Hub says what its branch keeps.
                self._post_news("hub", kept_line(subagent), task.id, details=subagent.report)
        finally:
            self._handled()

    def _subagent_over_time(self, subagent: Subagent) -> None:
        self._wake(subagent.task_id, over_time_event(subagent, self._clock.now()))

    def _wake(self, task_id: str, message: str, *, last_check: bool = False) -> None:
        """Queue news for the Orchestrator on a Task; a cancelled Task hears nothing more."""
        task = self._tasks.get(task_id)
        if task is None or task.cancelled:
            return
        task.queue_message()
        self._post(self._orchestrator_inbox, _OrchestratorInput(task.id, message, self._clock.now(), last_check))

    async def _watch(self, args: dict[str, Any]) -> str:
        task = self._tasks.get(self._orchestrating)
        if task is None or task.cancelled:
            return "watch works only while you are on a Task."
        watcher = self._waits.watch(task.id, str(args.get("kind") or ""), str(args.get("target") or ""))
        if isinstance(watcher, Refused):
            return watcher.reason
        # A Watcher ends on its own, so text mode waits for it like a Subagent.
        self._pending += 1
        self._settled.clear()
        return json.dumps(
            {
                "watcher_id": watcher.id,
                "watching": " ".join(watcher.command.watch),
                "note": "Its end reaches you as a watcher event. End this turn without a report unless the User "
                "should hear something now.",
            }
        )

    async def _schedule_check(self, args: dict[str, Any]) -> str:
        task = self._tasks.get(self._orchestrating)
        if task is None or task.cancelled:
            return "schedule_check works only while you are on a Task."
        note = str(args.get("note") or "")
        check = self._waits.schedule(task.id, args.get("after_seconds"), note, args.get("max_repeats"))
        if isinstance(check, Refused):
            return check.reason
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
        if self._orchestrating is None:
            return "cancel_wait works only while you are on a Task."
        cancelled = self._waits.cancel(args.get("wait_id"), task_id=self._orchestrating)
        return cancelled if isinstance(cancelled, str) else f"Cancelled {cancelled.id}."

    def _cancel_wait_for_user(self, wait_id: str) -> str:
        """The User stops one wait: its Task goes on, and the Orchestrator hears of it to decide what is next."""
        cancelled = self._waits.cancel(wait_id)
        if isinstance(cancelled, str):
            return cancelled
        self._wake(cancelled.task_id, cancelled_event(cancelled))
        return f"Cancelled {cancelled.id}."

    def _watcher_ended(self, watcher: Watcher) -> None:
        try:
            if watcher.state != "cancelled":
                self._wake(watcher.task_id, watcher_event(watcher))
        finally:
            self._handled()

    def _check_due(self, check: Check) -> None:
        self._wake(check.task_id, check_event(check), last_check=check.state == "done")
