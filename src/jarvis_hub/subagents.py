"""Subagents: the sessions that do the work, run by the Hub under a concurrency cap and a wall-clock limit."""

import asyncio
import contextlib
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, fields
from html import escape
from pathlib import Path
from typing import Any, Literal, Protocol, get_args

from jarvis_hub.agents.backend import (
    AgentBackend,
    AgentSession,
    Refused,
    SessionSpec,
    TextDelta,
    TextEnd,
    ToolCall,
    TurnEnd,
)
from jarvis_hub.config import Effort, Settings, is_effort
from jarvis_hub.limits import UsageLimit
from jarvis_hub.prompts import render
from jarvis_hub.subagent_rules import BRANCH_KINDS, branch_name, check_choice, connector_check, slug
from jarvis_hub.state import Fields, TimeShift, numbered, one_of

# Interrupted: the Hub stopped or crashed while it worked; its worktree and branch are kept.
SubagentState = Literal["queued", "running", "done", "failed", "cancelled", "interrupted"]
FINISHED: frozenset[SubagentState] = frozenset({"done", "failed", "cancelled", "interrupted"})
INTERRUPTED = "Interrupted: the Hub stopped while it worked, so it never finished."

# Kept out of a Subagent's tools altogether: the Hub owns the microphone and speaker.
VOICE_TOOLS = ("mcp__plugin_voicemode_voicemode", "mcp__voicemode")

TIME_LIMIT = 3600.0
# How long a stopped Subagent has to end its turn before the Hub drops its session.
STOP_GRACE = 10.0

# What a Subagent stopped by the usage limit is told when it may go on.
RESUME = "The usage limit has reset. Carry on from where you stopped."


class SubagentClock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class Refusal(Exception):
    """The workspace cannot give a Subagent what it asked for; the message says why, for the Orchestrator."""


class Workspace(Protocol):
    """Where Subagents work: the Org's clones and their worktrees. Never the User's own checkout."""

    async def repo(self, name: str) -> tuple[str, Path]:
        """The Org repo's name and clone, cloned first if missing. Raises Refusal outside the Org."""
        ...

    async def worktree(self, clone: Path, branch: str, holder: str) -> tuple[Path, str]:
        """A new worktree on a fresh branch named after `branch`, locked by `holder`: its path and branch."""
        ...

    async def scratch(self, clone: Path, name: str, holder: str) -> Path:
        """A new worktree with no branch, detached at the default branch, locked by `holder`."""
        ...

    async def release(self, worktree: Path, holder: str) -> str | None:
        """Releases the worktree's lock only while it names `holder`. None once `holder` holds no lock there, else
        why the lock was left: after a crash someone else may have taken the stale lock over."""
        ...

    async def describe(self, worktree: Path) -> str:
        """What is on the worktree's branch: commits not on the default branch or not pushed, uncommitted changes."""
        ...

    async def discard(self, worktree: Path, holder: str) -> str | None:
        """Releases and removes a scratch worktree `holder` locked. None once it is gone, else why it was kept."""
        ...


def lock_holder(subagent_id: str, task_id: str, title: str) -> str:
    """Who a Subagent's worktree lock names: the Hub releases only a lock that still names it."""
    return f"jarvis {subagent_id} ({task_id}): {title}"


@dataclass
class Subagent:
    id: str
    task_id: str
    repo: str
    title: str
    brief: str
    model: str
    effort: Effort
    cwd: Path
    created: float
    branch: str | None = None
    log: Path | None = None
    # The lock description saved with the Subagent, so a restart releases by what was written, not by a recomputed one.
    holder: str = ""
    state: SubagentState = "queued"
    started: float | None = None
    ended: float | None = None
    report: str = ""
    # The Approval this Subagent acts on, which widens its connector tools (ADR 0013).
    approval: str | None = None
    task_revision: int = 0
    stop: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def writes(self) -> bool:
        return self.branch is not None

    def __post_init__(self) -> None:
        self.holder = self.holder or lock_holder(self.id, self.task_id, self.title)

    def line(self, now: float) -> str:
        since = self.started if self.started is not None else self.created
        age = _minutes((self.ended or now) - since)
        where = f"{self.repo} on {self.branch}" if self.branch else f"{self.repo}, read-only"
        return f"{self.id} {self.state} {age}, {self.model} at {self.effort}, {where}: {self.title}"


def ended_event(subagent: Subagent) -> str:
    """The Orchestrator's news that a Subagent ended, carrying its final report."""
    return _event(subagent, subagent.state, subagent.report)


def over_time_event(subagent: Subagent, now: float) -> str:
    minutes = round((now - (subagent.started or subagent.created)) / 60)
    log = f" Its progress log is {subagent.log}." if subagent.log else ""
    text = (
        f"{subagent.id} has run {minutes} minutes and is still working.{log} Ask the User whether it should "
        "continue. It keeps going meanwhile; cancel_subagent stops it, and the Hub asks again after as long again."
    )
    return _event(subagent, "over_time", text)


def kept_line(subagent: Subagent) -> str:
    """What the User hears of a writing Subagent stopped with its cancelled Task."""
    return f"{subagent.id} on {subagent.task_id} stopped; its branch {subagent.branch} is kept, details on screen."


def _event(subagent: Subagent, status: str, text: str) -> str:
    return (
        f'<event source="subagent" id="{subagent.id}" task="{subagent.task_id}" status="{status}">'
        f"{escape(text, quote=False)}</event>"
    )


@dataclass(frozen=True)
class SubagentSetup:
    session: Callable[[Subagent], SessionSpec]
    workspace: Workspace
    # One JSONL progress log per Subagent goes here; None keeps no log.
    log_dir: Path | None = None
    max_effort: Effort = "max"
    cap: int = 3
    time_limit: float = TIME_LIMIT


class Subagents:
    """The registry and runner. `on_end` hears of every Subagent that ends, `on_over_time` of each limit passed, and
    `on_turn` of each turn's result with its duration, and `on_start` of each leaving the queue. While `limit` is
    active nothing new starts."""

    def __init__(
        self,
        *,
        setup: SubagentSetup,
        backend: AgentBackend,
        clock: SubagentClock,
        on_end: Callable[[Subagent], None],
        on_over_time: Callable[[Subagent], None],
        on_turn: Callable[[Subagent, SessionSpec, TurnEnd, int], None],
        limit: UsageLimit,
        on_start: Callable[[Subagent], None] = lambda subagent: None,
    ) -> None:
        self._setup = setup
        self._backend = backend
        self._clock = clock
        self._on_end = on_end
        self._on_over_time = on_over_time
        self._on_turn = on_turn
        self._on_start = on_start
        self._limit = limit
        self._slots = asyncio.Semaphore(setup.cap)
        self._all: dict[str, Subagent] = {}
        self._ids = 0
        self._runs: list[asyncio.Task[None]] = []
        self._closing = False

    async def prepare(self, task_id: str, args: dict[str, Any], user_words: Sequence[str], *, task_revision: int = 0) -> Subagent | Refused:
        """Check the request and set up its worktree, with a branch when it writes. Not started yet."""
        repo, title, brief = (str(args.get(k) or "").strip() for k in ("repo", "title", "brief"))
        # worktree.sh keeps only a lock description's first line, so a title is one line of plain spaces.
        title = " ".join(title.split())
        if not (repo and title and brief):
            return Refused("repo, title and brief are required.")
        model, effort = str(args.get("model") or "").strip(), str(args.get("effort") or "").strip()
        verdict = check_choice(model, effort, max_effort=self._setup.max_effort, user_words=user_words)
        if verdict is not None:
            return verdict
        assert is_effort(effort)
        writes = args.get("writes") in (True, "true")
        kind = str(args.get("branch_kind") or "feat").strip()
        if writes and kind not in BRANCH_KINDS:
            return Refused(f"branch_kind is one of {', '.join(BRANCH_KINDS)}.")
        # Taken before the first await, so two spawns in flight never share an id.
        self._ids += 1
        sid = f"S{self._ids}"
        holder = lock_holder(sid, task_id, title)
        workspace = self._setup.workspace
        try:
            name, clone = await workspace.repo(repo)
            branch = None
            if writes:
                cwd, branch = await workspace.worktree(clone, branch_name(kind, title), holder)
            else:
                cwd = await workspace.scratch(clone, f"read-{slug(title)}", holder)
        except Refusal as e:
            return Refused(str(e))
        log = self._setup.log_dir / f"{sid}.jsonl" if self._setup.log_dir else None
        subagent = Subagent(sid, task_id, name, title, brief, model, effort, cwd, self._clock.now(), branch, log, holder, task_revision=task_revision)
        self._all[sid] = subagent
        return subagent

    def start(self, subagent: Subagent) -> SubagentState:
        """Runs it now, or queues it behind the cap or the usage limit: which of the two it says."""
        ahead = [s for s in self._all.values() if s is not subagent and s.state not in FINISHED and s.started is None]
        running = [s for s in self._all.values() if s.state == "running"]
        self._runs.append(asyncio.create_task(self._run(subagent)))
        return "queued" if self._limit.active or len(running) + len(ahead) >= self._setup.cap else "running"

    def get(self, subagent_id: object) -> Subagent | None:
        return self._all.get(str(subagent_id).strip().upper()) if subagent_id else None

    def live(self, task_id: str) -> list[Subagent]:
        return [s for s in self._all.values() if s.task_id == task_id and s.state not in FINISHED]

    def lines(self, task_id: str | None = None, *, revision: int | None = None) -> list[str]:
        now = self._clock.now()
        return [s.line(now) for s in self._all.values()
                if (task_id is None or s.task_id == task_id) and (revision is None or s.task_revision == revision)]

    def listing(self) -> str:
        if not self._all:
            return "No Subagents this session."
        now = self._clock.now()
        rows: list[str] = []
        for s in self._all.values():
            row = f"{s.line(now)} | Task {s.task_id} | folder {s.cwd}"
            if s.log:
                row += f" | log {s.log}"
            if s.report:
                row += f" | report: {_first_line(s.report)}"
            rows.append(row)
        return "\n".join(rows)

    def cancel(self, subagent_id: object) -> str:
        subagent = self.get(subagent_id)
        if subagent is None:
            return f"There is no Subagent {subagent_id}."
        if subagent.state in FINISHED:
            return f"{subagent.id} is already {subagent.state}."
        subagent.stop.set()
        kept = " Its worktree and branch are kept." if subagent.writes else ""
        return f"Stopping {subagent.id}; its report follows as an event.{kept}"

    def dump(self, task_ids: set[str], to_wall: TimeShift) -> list[dict[str, Any]]:
        """The Subagents of these Tasks, for the state file."""
        return [_SAVED.dump(s, to_wall) for s in self._all.values() if s.task_id in task_ids]

    @staticmethod
    def load(records: list[dict[str, Any]], from_wall: TimeShift) -> list[Subagent]:
        """Saved Subagents, checked whole and not yet restored."""
        subagents: list[Subagent] = []
        for r in records:
            subagent = Subagent(**_SAVED.load({"task_revision": 0, **r}, from_wall))
            numbered(subagent.id, "S")
            one_of(subagent.state, get_args(SubagentState))
            if not is_effort(subagent.effort):
                raise ValueError(f"{subagent.effort!r} is not an effort")
            subagents.append(subagent)
        return subagents

    def restore(self, subagents: list[Subagent]) -> list[Subagent]:
        """Brings back loaded Subagents. One the old Hub never saw end is marked interrupted and returned, its
        worktree still to free with `free_interrupted`; one that never started stays queued for `start`."""
        cut_off: list[Subagent] = []
        for subagent in subagents:
            self._all[subagent.id] = subagent
            self._ids = max(self._ids, numbered(subagent.id, "S"))
            if subagent.state == "queued" and subagent.started is None:
                continue
            if subagent.state not in FINISHED:
                subagent.state = "interrupted"
                cut_off.append(subagent)
        return cut_off

    def queued(self) -> list[Subagent]:
        """Subagents not started yet, in the order they were created: after `restore`, the ones to start again."""
        return [s for s in self._all.values() if s.state == "queued" and s.started is None]

    async def free_interrupted(self, subagent: Subagent) -> None:
        """Releases the lock of a Subagent cut off by a crash, as its own end would have: the session holding it is
        gone, and a lock left behind would keep sweep off the worktree for good."""
        report = INTERRUPTED
        try:
            report += await self._free(subagent)
        finally:
            subagent.report, subagent.ended = report, self._clock.now()

    async def close(self) -> None:
        """Stop every Subagent and wait for each to end, cutting off any that will not; worktrees and branches stay.
        A Subagent still at work ends interrupted, not cancelled."""
        self._closing = True
        for subagent in self._all.values():
            subagent.stop.set()
        runs = [run for run in self._runs if not run.done()]
        if not runs:
            return
        _, stuck = await asyncio.wait(runs, timeout=2 * STOP_GRACE)
        for run in stuck:
            run.cancel()
        if stuck:
            # A cancelled run still ends by describing and discarding its worktree; a hung git must not block shutdown.
            await asyncio.wait(stuck, timeout=STOP_GRACE)

    async def _run(self, subagent: Subagent) -> None:
        end, text = TurnEnd("interrupted"), ""
        held = False
        try:
            await self._limit.wait_clear(subagent.stop)
            held = await self._take_slot(subagent)
            if held and not subagent.stop.is_set():
                subagent.state, subagent.started = "running", self._clock.now()
                self._on_start(subagent)
                end, text = await self._session(subagent)
        except Exception as e:
            end = TurnEnd("failed", error=f"{type(e).__name__}: {e}")
        except BaseException:
            # Cancelled from outside, as close() does with a run that will not stop: it still ends properly.
            subagent.stop.set()
            raise
        finally:
            if held:
                self._slots.release()
            await self._finish(subagent, end, text)

    async def _take_slot(self, subagent: Subagent) -> bool:
        """Waits for a free slot, or for a stop while queued. True when the slot is held."""
        taking = asyncio.create_task(self._slots.acquire())
        stopping = asyncio.create_task(subagent.stop.wait())
        try:
            await asyncio.wait([taking, stopping], return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
            if not taking.done():
                taking.cancel()
                await asyncio.wait([taking])
        return taking.done() and not taking.cancelled()

    async def _session(self, subagent: Subagent) -> tuple[TurnEnd, str]:
        log = _Log(subagent.log, self._clock)
        spec = self._setup.session(subagent)
        log.write(
            "start", model=spec.model, effort=spec.effort, cwd=str(spec.cwd), branch=subagent.branch, brief=subagent.brief
        )
        session = await self._open(spec, subagent.stop)
        if session is None:
            return TurnEnd("interrupted"), ""
        consuming = asyncio.create_task(self._consume(session, spec, subagent, log))
        stopping = asyncio.create_task(subagent.stop.wait())
        timing = asyncio.create_task(self._watch_time(subagent))
        try:
            await asyncio.wait([consuming, stopping], return_when=asyncio.FIRST_COMPLETED)
            if not consuming.done():
                with contextlib.suppress(Exception):
                    await session.interrupt()
                await asyncio.wait([consuming], timeout=STOP_GRACE)
            if not consuming.done():
                consuming.cancel()
                await asyncio.wait([consuming])
                return TurnEnd("interrupted"), ""
            return consuming.result()
        finally:
            for waiting in (consuming, stopping, timing):
                waiting.cancel()
            with contextlib.suppress(Exception):
                await session.close()

    async def _open(self, spec: SessionSpec, stop: asyncio.Event) -> AgentSession | None:
        """The opened session, or None when a stop comes first: an open can hang on a CLI that never answers."""
        opening = asyncio.create_task(self._backend.open(spec))
        stopping = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait([opening, stopping], return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
            if not opening.done():
                opening.cancel()
                await asyncio.wait([opening])
        if opening.cancelled():
            return None
        session = opening.result()
        if stop.is_set():
            with contextlib.suppress(Exception):
                await session.close()
            return None
        return session

    async def _consume(
        self, session: AgentSession, spec: SessionSpec, subagent: Subagent, log: "_Log"
    ) -> tuple[TurnEnd, str]:
        """The last turn's end, and the text after its last tool call: the Subagent's report. A turn the usage limit
        stopped is sent again, as a request to carry on, once the limit has reset."""
        message = subagent.brief
        while True:
            started = self._clock.now()
            end, final = await self._turn(session, message, log)
            self._on_turn(subagent, spec, end, round((self._clock.now() - started) * 1000))
            self._limit.observe("subagent", end)
            if end.outcome != "limited":
                return end, final
            await self._limit.wait_clear(subagent.stop)
            if subagent.stop.is_set():
                return TurnEnd("interrupted"), ""
            message = RESUME

    async def _turn(self, session: AgentSession, message: str, log: "_Log") -> tuple[TurnEnd, str]:
        end = TurnEnd("failed", error="the session ended without a result")
        block, final = "", ""
        async for event in session.send(message):
            match event:
                case TextDelta(text):
                    block += text
                    final += text
                case TextEnd():
                    log.write("text", text=block)
                    block = ""
                case ToolCall(name, args):
                    log.write("tool", name=name, input=args)
                    final = ""
                case TurnEnd():
                    end = event
        log.write("end", outcome=end.outcome, error=end.error, usage=asdict(end.usage), resets_at=end.resets_at)
        return end, final.strip()

    async def _watch_time(self, subagent: Subagent) -> None:
        while True:
            await self._clock.sleep(self._setup.time_limit)
            if not subagent.stop.is_set():
                self._on_over_time(subagent)

    async def _finish(self, subagent: Subagent, end: TurnEnd, text: str) -> None:
        """Settles its state and report, frees its worktree and tells `on_end`, whatever fails on the way."""
        stopped = subagent.stop.is_set()
        if self._closing and stopped and subagent.started is None and end.outcome != "failed":
            # Nothing done yet, so nothing for the Orchestrator to judge: it stays queued, worktree and lock kept,
            # and the next launch starts it.
            self._on_end(subagent)
            return
        if end.outcome == "completed":
            state: SubagentState = "done"
            report = text or "It ended without a report."
        elif self._closing and stopped and end.outcome != "failed":
            state, report = "interrupted", INTERRUPTED
        elif end.outcome == "interrupted" and stopped:
            state = "cancelled"
            report = "Cancelled before it finished." if subagent.started else "Cancelled before it started."
        else:
            state = "failed"
            reason = end.error or "it stopped without the Hub stopping it"
            report = f"Failed: {reason}." + (f"\n\n{text}" if text else "")
        try:
            report += await self._free(subagent)
        finally:
            subagent.state, subagent.report, subagent.ended = state, report, self._clock.now()
            self._on_end(subagent)

    async def _free(self, subagent: Subagent) -> str:
        """Releases a writing Subagent's worktree and says what its branch keeps; removes a reading one's."""
        workspace = self._setup.workspace
        if not subagent.writes:
            try:
                kept = await workspace.discard(subagent.cwd, subagent.holder)
            except Exception as e:
                kept = str(e)
            return f"\n\nIts read-only worktree {subagent.cwd} was kept: {kept}" if kept else ""
        try:
            left = await workspace.release(subagent.cwd, subagent.holder)
        except Exception as e:
            left = f"its lock could not be released: {e}"
        try:
            on_branch = await workspace.describe(subagent.cwd)
        except Exception as e:
            on_branch = f"Could not read the branch: {e}"
        lock = f"\nIts worktree was not unlocked: {left}" if left else ""
        return f"\n\nBranch {subagent.branch}, kept in {subagent.cwd}:\n{on_branch}{lock}"


_SAVED = Fields(
    tuple(f.name for f in fields(Subagent) if f.name != "stop"),
    times=frozenset({"created", "started", "ended"}),
    paths=frozenset({"cwd", "log"}),
)


class _Log:
    def __init__(self, path: Path | None, clock: SubagentClock) -> None:
        self._path = path
        self._clock = clock
        self._t0: float = clock.now()

    def write(self, kind: str, **fields: Any) -> None:
        if self._path is None:
            return
        row = {"t": round(self._clock.now() - self._t0, 3), "kind": kind, **fields}
        # A log that cannot be written must not stop the work it records.
        with contextlib.suppress(OSError):
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def subagent_session(settings: Settings, subagent: Subagent) -> SessionSpec:
    """The Subagent's session: Claude Code's prompt plus the preamble, the User's settings and skills, no prompts."""
    if subagent.branch:
        branch_line = (
            f"Branch: `{subagent.branch}`, in a worktree made for you and locked under {subagent.id}. "
            "Commit here. The Hub releases the lock when you end, so leave the lock and the worktree in place."
        )
    else:
        branch_line = (
            "You only read: this is a worktree made for you with no branch, detached at the default branch, and the "
            "Hub removes it when you end. Check out any other ref you need to read here, and change nothing."
        )
    values = {
        "id": subagent.id,
        "user": settings.user_name or "the User",
        "org": settings.org,
        "repo": subagent.repo,
        "cwd": str(subagent.cwd),
        "branch_line": branch_line,
        "preferences": str(settings.repo_dir / "docs" / "working-preferences.md"),
    }
    return SessionSpec(
        role="subagent",
        model=subagent.model,
        effort=subagent.effort,
        thinking=True,
        system_prompt="\n".join(render(name, values) for name in ("subagent.md", "boundary.md")),
        cwd=subagent.cwd,
        preset=True,
        skip_permissions=True,
        denied_tools=VOICE_TOOLS,
        mcp_guard=connector_check,
    )


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else ""


def _minutes(seconds: float) -> str:
    return f"{max(0, round(seconds))} s" if seconds < 90 else f"{round(seconds / 60)} min"
