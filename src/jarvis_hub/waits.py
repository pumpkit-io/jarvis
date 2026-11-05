"""Watchers and Checks (ADR 0003): a Task waits on a fixed gh command or a timer, never on a model call.
The Orchestrator names only a kind and a target, never a command."""

import asyncio
import contextlib
import os
import re
import signal
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html import escape
from typing import Any, Literal, Protocol

from jarvis_hub.agents.backend import Refused
from jarvis_hub.read_only import Argv, confined
from jarvis_hub.state import Fields, TimeShift, numbered
from jarvis_hub.tasks import ago

WATCH_KINDS: dict[str, str] = {"ci_run": "CI run", "pr_checks": "checks on PR"}
WatcherState = Literal["waiting", "succeeded", "failed", "timed_out", "error", "cancelled"]
CheckState = Literal["waiting", "done", "cancelled"]

# gh asks GitHub again every this many seconds while it watches: calls to the API, never to a model.
GH_INTERVAL = "30"
# A run queued behind a stuck runner can wait for days; a Watcher gives up after this and says so.
WATCH_LIMIT = 6 * 3600.0
MIN_AFTER = 60.0
# Each repeat waits twice as long as the one before, up to this; the first wait is capped there too.
MAX_INTERVAL = 4 * 3600.0
MAX_REPEATS = 10
MAX_WATCHERS = 5
MAX_CHECKS = 5
# The last lines a Watcher's command printed travel with its event; gh's progress screens before them do not.
OUTPUT_LINES = 20

# `pumpkit#42` or `pumpkit-io/pumpkit#42`; 16 digits is far past any run id yet still a 64-bit integer for gh.
_TARGET = re.compile(r"(?:(?P<owner>[A-Za-z0-9-]+)/)?(?P<repo>[A-Za-z0-9._-]+)#(?P<number>[1-9][0-9]{0,15})")
# What `gh pr checks` prints, exiting 1 like a failing check, for a PR whose branch has no checks at all.
_NO_CHECKS = "no checks reported"

Runner = Callable[[Argv], Awaitable[tuple[int, str]]]


class WaitClock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


async def run_confined(words: Argv) -> tuple[int, str]:
    """Runs gh with no shell behind the read-only guard's env -i (ADR 0007): exit code and output tail.
    Its own process group keeps a Ctrl-C meant for the Hub from failing it, and a cancel kills the whole group."""
    process = await asyncio.create_subprocess_exec(
        *confined(words),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    tail: deque[str] = deque(maxlen=OUTPUT_LINES)
    try:
        assert process.stdout is not None
        async for raw in process.stdout:
            if line := raw.decode(errors="replace").rstrip():
                tail.append(line)
        code = await process.wait()
    except BaseException:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        raise
    return code, "\n".join(tail)


@dataclass(frozen=True)
class WatchCommand:
    """`target` as `repo#number`; `view` asks gh whether it exists before `watch` waits on it."""

    target: str
    view: Argv
    watch: Argv


def watch_command(kind: str, target: str, org: str) -> WatchCommand | Refused:
    if kind not in WATCH_KINDS:
        return Refused(f"kind is one of {', '.join(WATCH_KINDS)}.")
    match = _TARGET.fullmatch(target.strip())
    if match is None or match["repo"] in (".", ".."):
        return Refused("target is an Org repo and a number, such as pumpkit#42.")
    if match["owner"] and match["owner"].lower() != org.lower():
        return Refused(f"{target} is outside the Org {org}.")
    repo, number = match["repo"], match["number"]
    where = ("-R", f"{org}/{repo}")
    if kind == "ci_run":
        view = ("gh", "run", "view", number, *where, "--json", "status")
        watch = ("gh", "run", "watch", number, *where, "--exit-status", "--compact", "--interval", GH_INTERVAL)
    else:
        view = ("gh", "pr", "view", number, *where, "--json", "number")
        watch = ("gh", "pr", "checks", number, *where, "--watch", "--fail-fast", "--interval", GH_INTERVAL)
    return WatchCommand(f"{repo}#{number}", view, watch)


@dataclass
class Watcher:
    id: str
    task_id: str
    kind: str
    command: WatchCommand
    created: float
    state: WatcherState = "waiting"
    ended: float | None = None
    output: str = ""
    task_revision: int = 0

    def line(self, now: float) -> str:
        what = f"{WATCH_KINDS[self.kind]} {self.command.target}"
        age = ago((self.ended or now) - self.created)
        if self.state == "waiting":
            return f"{self.id} watching the {what}, {age} so far"
        return f"{self.id} {self.state.replace('_', ' ')} after {age}: the {what}"


@dataclass
class Check:
    id: str
    task_id: str
    note: str
    created: float
    due: float
    interval: float
    max_repeats: int
    repeats: int = 0
    state: CheckState = "waiting"
    task_revision: int = 0

    def line(self, now: float) -> str:
        if self.state == "waiting":
            return f"{self.id} check in {ago(self.due - now)} ({self.repeats + 1} of {self.max_repeats}): {self.note}"
        return f"{self.id} check {self.state} after {self.repeats} of {self.max_repeats}: {self.note}"


Wait = Watcher | Check

_WATCHER = Fields(("id", "task_id", "kind", "created", "task_revision"), times=frozenset({"created"}))
_CHECK = Fields(
    ("id", "task_id", "note", "created", "due", "interval", "max_repeats", "repeats", "task_revision"), times=frozenset({"created", "due"})
)


def watcher_event(watcher: Watcher) -> str:
    """The Orchestrator's news that a Watcher ended, with the last of what its command printed."""
    text = watcher.output or "No output."
    return (
        f'<event source="watcher" id="{watcher.id}" task="{watcher.task_id}" kind="{watcher.kind}" '
        f'target="{watcher.command.target}" status="{watcher.state}">{escape(text, quote=False)}</event>'
    )


def check_event(check: Check) -> str:
    text = check.note
    if check.state == "done":
        text += " This is the last time: report what you found, or schedule another Check."
    return (
        f'<event source="timer" id="{check.id}" task="{check.task_id}" repeat="{check.repeats}" '
        f'of="{check.max_repeats}">{escape(text, quote=False)}</event>'
    )


def cancelled_event(wait: "Wait") -> str:
    """The Orchestrator's news that the User stopped one of its waits."""
    text = f"The User cancelled {wait.id}; nothing more comes from it."
    return f'<event source="hub" id="{wait.id}" task="{wait.task_id}" status="cancelled">{text}</event>'


@dataclass(frozen=True)
class WaitSetup:
    org: str
    runner: Runner = run_confined
    max_watchers: int = MAX_WATCHERS
    max_checks: int = MAX_CHECKS
    watch_limit: float = WATCH_LIMIT


class Waits:
    """Every Watcher and Check this launch. `on_watcher_end` hears once of every Watcher that stops, however it
    stopped; `on_check` of every time a Check comes due."""

    def __init__(
        self,
        *,
        setup: WaitSetup,
        clock: WaitClock,
        on_watcher_end: Callable[[Watcher], None],
        on_check: Callable[[Check], None],
    ) -> None:
        self._setup = setup
        self._clock = clock
        self._on_watcher_end = on_watcher_end
        self._on_check = on_check
        self._all: dict[str, Wait] = {}
        self._runs: dict[str, asyncio.Task[None]] = {}
        self._watchers = 0
        self._checks = 0
        self._closing = False

    def watch(self, task_id: str, kind: str, target: str) -> Watcher | Refused:
        command = watch_command(kind, target, self._setup.org)
        if isinstance(command, Refused):
            return command
        if len(self._waiting(Watcher)) >= self._setup.max_watchers:
            return Refused(f"{self._setup.max_watchers} Watchers already wait; cancel one with cancel_wait first.")
        self._watchers += 1
        watcher = Watcher(f"W{self._watchers}", task_id, kind, command, self._clock.now())
        self._start(watcher, self._run_watcher(watcher))
        # A done callback, so a Watcher cancelled before its run began is still reported once.
        self._runs[watcher.id].add_done_callback(lambda _: self._watcher_stopped(watcher))
        return watcher

    def schedule(
        self, task_id: str, after: float | str | None, note: str, max_repeats: int | str | None
    ) -> Check | Refused:
        """Sets a Check, its first wait clamped to MIN_AFTER..MAX_INTERVAL and its repeats to 1..MAX_REPEATS."""
        note = note.strip()
        if not note:
            return Refused("note is required: what to look at when the Check comes due.")
        try:
            seconds = min(max(float(after or 0), MIN_AFTER), MAX_INTERVAL)
            repeats = min(max(int(max_repeats or 1), 1), MAX_REPEATS)
        except (TypeError, ValueError):
            return Refused("after_seconds and max_repeats are numbers.")
        if len(self._waiting(Check)) >= self._setup.max_checks:
            return Refused(f"{self._setup.max_checks} Checks already wait; cancel one with cancel_wait first.")
        self._checks += 1
        now = self._clock.now()
        check = Check(f"C{self._checks}", task_id, note, now, now + seconds, seconds, repeats)
        self._start(check, self._run_check(check))
        return check

    def get(self, wait_id: object) -> Wait | None:
        return self._all.get(str(wait_id).strip().upper()) if wait_id else None

    def cancel(self, wait_id: object, *, task_id: str | None = None) -> Wait | str:
        """The Watcher or Check now cancelled, or why nothing was. With `task_id`, only a wait of that Task."""
        wait = self.get(wait_id)
        if wait is None or (task_id is not None and wait.task_id != task_id):
            return f"There is no Watcher or Check {wait_id}{f' on {task_id}' if task_id else ''}."
        if wait.state != "waiting":
            return f"{wait.id} is already {wait.state.replace('_', ' ')}."
        wait.state = "cancelled"
        if (run := self._runs.get(wait.id)) is not None:
            run.cancel()
        elif isinstance(wait, Watcher):
            # Restored and not yet resumed: no run will report its end, so it is reported here.
            self._watcher_stopped(wait)
        return wait

    def cancel_task(self, task_id: str) -> None:
        for wait in self.live(task_id):
            self.cancel(wait.id)

    def live(self, task_id: str) -> list[Wait]:
        return [w for w in self._all.values() if w.task_id == task_id and w.state == "waiting"]

    def lines(self, task_id: str, *, revision: int | None = None) -> list[str]:
        now = self._clock.now()
        return [w.line(now) for w in self._all.values()
                if w.task_id == task_id and (revision is None or w.task_revision == revision)]

    async def close(self) -> None:
        """Stops every wait's run and reports none of them: a wait still waiting stays so, kept for the next launch."""
        self._closing = True
        runs = [run for run in self._runs.values() if not run.done()]
        for run in runs:
            run.cancel()
        if runs:
            await asyncio.wait(runs)

    def dump(self, to_wall: TimeShift) -> list[dict[str, Any]]:
        """The waits still waiting, for the state file."""
        records: list[dict[str, Any]] = []
        for w in self._all.values():
            if isinstance(w, Watcher) and w.state == "waiting":
                records.append({**_WATCHER.dump(w, to_wall), "target": w.command.target})
            elif isinstance(w, Check) and w.state == "waiting":
                records.append(_CHECK.dump(w, to_wall))
        return records

    def load(self, records: list[dict[str, Any]], from_wall: TimeShift) -> tuple[list[Wait], list[str]]:
        """Saved waits, checked whole and not yet restored, and a line per wait that cannot be. A wall clock set
        back while the Hub was down never stretches a Watcher's limit or a Check's wait."""
        now = self._clock.now()
        waits: list[Wait] = []
        problems: list[str] = []
        for r in records:
            if "kind" in r:
                values = _WATCHER.load({"task_revision": 0, **r}, from_wall)
                numbered(values["id"], "W")
                command = watch_command(values["kind"], r["target"], self._setup.org)
                if isinstance(command, Refused):
                    problems.append(f"{values['id']} on {values['task_id']} could not be restored: {command.reason}")
                    continue
                values["created"] = min(values["created"], now)
                waits.append(Watcher(**values, command=command))
            else:
                check = Check(**_CHECK.load({"task_revision": 0, **r}, from_wall))
                numbered(check.id, "C")
                if not all(isinstance(r[k], (int, float)) for k in ("interval", "max_repeats", "repeats")):
                    raise ValueError(f"{check.id} has a count or interval that is not a number")
                check.created, check.due = min(check.created, now), min(check.due, now + check.interval)
                waits.append(check)
        return waits, problems

    def restore(self, waits: list[Wait]) -> None:
        """Takes back loaded waits without starting them, so the recovery message can go first: see `resume`."""
        for wait in waits:
            self._all[wait.id] = wait
            if isinstance(wait, Watcher):
                self._watchers = max(self._watchers, numbered(wait.id, "W"))
            else:
                self._checks = max(self._checks, numbered(wait.id, "C"))

    def resume(self, *, only: set[str] | None = None) -> None:
        """Starts the restored waits: a Watcher reruns its gh command, checked anew, within what is left of its
        limit; a Check keeps its due time, so one overdue fires at once, once."""
        if self._closing:
            return
        for wait in self._all.values():
            if wait.state != "waiting" or wait.id in self._runs or (only is not None and wait.id not in only):
                continue
            if isinstance(wait, Watcher):
                self._start(wait, self._run_watcher(wait))
                self._runs[wait.id].add_done_callback(lambda _, w=wait: self._watcher_stopped(w))
            else:
                self._start(wait, self._run_check(wait))

    def _waiting(self, kind: type[Watcher] | type[Check]) -> list[Wait]:
        return [w for w in self._all.values() if isinstance(w, kind) and w.state == "waiting"]

    def _start(self, wait: Wait, run: Awaitable[None]) -> None:
        self._all[wait.id] = wait
        self._runs[wait.id] = asyncio.ensure_future(run)

    async def _run_watcher(self, watcher: Watcher) -> None:
        try:
            running = asyncio.ensure_future(self._view_then_watch(watcher))
            left = self._setup.watch_limit - (self._clock.now() - watcher.created)
            limit = asyncio.ensure_future(self._clock.sleep(left))
            try:
                await asyncio.wait([running, limit], return_when=asyncio.FIRST_COMPLETED)
            finally:
                limit.cancel()
                if not running.done():
                    running.cancel()
                    await asyncio.wait([running])
            if running.cancelled():
                watcher.state, watcher.output = "timed_out", f"Still not over after {ago(self._setup.watch_limit)}."
            elif (error := running.exception()) is not None:
                watcher.state, watcher.output = "error", f"{type(error).__name__}: {error}"
            else:
                watcher.state, watcher.output = running.result()
        except asyncio.CancelledError:
            if not self._closing:
                watcher.state = "cancelled"
            raise

    async def _view_then_watch(self, watcher: Watcher) -> tuple[WatcherState, str]:
        """gh exits 1 for a red run and for its own errors alike, so the target is looked up before it is watched."""
        code, printed = await self._setup.runner(watcher.command.view)
        if code != 0:
            return "error", printed
        code, printed = await self._setup.runner(watcher.command.watch)
        if code == 0:
            return "succeeded", printed
        return ("error" if _NO_CHECKS in printed else "failed"), printed

    def _watcher_stopped(self, watcher: Watcher) -> None:
        if self._closing:
            return
        watcher.ended = self._clock.now()
        self._on_watcher_end(watcher)

    async def _run_check(self, check: Check) -> None:
        while check.state == "waiting":
            await self._clock.sleep(check.due - self._clock.now())
            check.repeats += 1
            if check.repeats >= check.max_repeats:
                check.state = "done"
            else:
                check.interval = min(check.interval * 2, MAX_INTERVAL)
                check.due = self._clock.now() + check.interval
            self._on_check(check)

