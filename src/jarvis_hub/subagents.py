"""Subagents: the sessions that do the work, run by the Hub under a concurrency cap and a wall-clock limit."""

import asyncio
import contextlib
import json
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

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
from jarvis_hub.config import EFFORTS, Effort, Settings, is_effort
from jarvis_hub.prompts import render

SubagentState = Literal["queued", "running", "done", "failed", "cancelled"]
FINISHED: frozenset[SubagentState] = frozenset({"done", "failed", "cancelled"})
BRANCH_KINDS = ("feat", "fix", "chore")

# Ticket #4 found every model accepts every effort level, so the real limits are the cap and the User's ask.
SUPPORTED_EFFORTS: dict[str, frozenset[Effort]] = {
    "haiku": frozenset(EFFORTS),
    "sonnet": frozenset(EFFORTS),
    "opus": frozenset(EFFORTS),
}
# The levels that spend the User's limits fastest, used only when their words name one (story 41).
ON_REQUEST: dict[Effort, re.Pattern[str]] = {
    "xhigh": re.compile(r"\bx-?high\b|\bextra[- ]high\b", re.IGNORECASE),
    "max": re.compile(r"\bmax(imum)?\b|\bmassim[oa]\b", re.IGNORECASE),
}

# Connector tools that only read. Every other MCP tool is refused in a Subagent until an Approval covers it (ADR 0008).
READ_CONNECTOR_TOOLS = frozenset(
    f"mcp__{server}__{tool}"
    for server, tools in {
        "claude_ai_Pumpkit": ("get_post", "list_posts", "get_usage", "list_inspiration_authors", "list_past_communities"),
        "pumpkit": ("get_post", "list_posts", "get_usage", "list_inspiration_authors", "list_past_communities"),
        "claude_ai_Claude_Docs": ("guide", "read", "query"),
    }.items()
    for tool in tools
)
# Kept out of a Subagent's tools altogether: the Hub owns the microphone and speaker.
VOICE_TOOLS = ("mcp__plugin_voicemode_voicemode", "mcp__voicemode")

TIME_LIMIT = 3600.0
# How long a stopped Subagent has to end its turn before the Hub drops its session.
STOP_GRACE = 10.0


def check_choice(model: str, effort: str, *, max_effort: Effort, user_words: Sequence[str]) -> Refused | None:
    """Whether a Subagent may run on this model at this effort, given the cap and what the User has said."""
    family = next((f for f in SUPPORTED_EFFORTS if re.fullmatch(rf"(claude-)?{f}(-[\w.-]+)?", model)), None)
    if family is None:
        return Refused(f"{model!r} is not a model; use haiku, sonnet or opus.")
    if not is_effort(effort):
        return Refused(f"{effort!r} is not an effort; use one of {', '.join(EFFORTS)}.")
    level = effort
    if level not in SUPPORTED_EFFORTS[family]:
        return Refused(f"{family} does not run at {level}.")
    if EFFORTS.index(level) > EFFORTS.index(max_effort):
        return Refused(f"{level} is above JARVIS_MAX_EFFORT={max_effort}; use {max_effort} or lower.")
    if level in ON_REQUEST:
        asked = [lvl for lvl, pattern in ON_REQUEST.items() if any(pattern.search(w) for w in user_words)]
        if not any(EFFORTS.index(a) >= EFFORTS.index(level) for a in asked):
            return Refused(f"{level} is used only when the User asks for it; use high or lower.")
    return None


def branch_name(kind: str, title: str) -> str:
    """An sdlc-style branch for the work: `<kind>/<slug of the title>`."""
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:40].strip("-")
    return f"{kind}/{slug or 'work'}"


def connector_check(tool_name: str) -> Refused | None:
    if tool_name in READ_CONNECTOR_TOOLS:
        return None
    return Refused(
        f"{tool_name} can change something outside the repos, so a Subagent may call it only with an Approval "
        "from the User, and this brief carries none. Report what you would do with it instead."
    )


class SubagentClock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class Refusal(Exception):
    """The workspace cannot give a Subagent what it asked for; the message says why, for the Orchestrator."""


class Workspace(Protocol):
    """Where Subagents work: the Org's clones and their worktrees."""

    async def repo(self, name: str) -> tuple[str, Path]:
        """The Org repo's name and clone, cloned first if missing. Raises Refusal outside the Org."""
        ...

    async def worktree(self, clone: Path, branch: str, holder: str) -> tuple[Path, str]:
        """A new worktree on a fresh branch named after `branch`, locked by `holder`: its path and branch."""
        ...

    async def release(self, worktree: Path) -> None: ...

    async def describe(self, worktree: Path) -> str:
        """What is on the worktree's branch: commits not on the default branch, and uncommitted changes."""
        ...


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
    state: SubagentState = "queued"
    started: float | None = None
    ended: float | None = None
    report: str = ""
    # How many times the wall-clock limit has passed.
    over_time: int = 0
    stop: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def writes(self) -> bool:
        return self.branch is not None

    def line(self, now: float) -> str:
        since = self.started if self.started is not None else self.created
        age = _minutes((self.ended or now) - since)
        where = f"{self.repo} on {self.branch}" if self.branch else f"{self.repo}, read-only"
        return f"{self.id} {self.state} {age}, {self.model} at {self.effort}, {where}: {self.title}"


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
    """The registry and runner. `on_end` hears of every Subagent that ends, `on_over_time` of each limit passed."""

    def __init__(
        self,
        *,
        setup: SubagentSetup,
        backend: AgentBackend,
        clock: SubagentClock,
        on_end: Callable[[Subagent], None],
        on_over_time: Callable[[Subagent], None],
    ) -> None:
        self._setup = setup
        self._backend = backend
        self._clock = clock
        self._on_end = on_end
        self._on_over_time = on_over_time
        self._slots = asyncio.Semaphore(setup.cap)
        self._all: dict[str, Subagent] = {}
        self._runs: list[asyncio.Task[None]] = []

    async def prepare(self, task_id: str, args: dict[str, Any], user_words: Sequence[str]) -> Subagent | Refused:
        """Check the request and set up its folder (a clone, and a worktree when it writes). Not started yet."""
        repo, title, brief = (str(args.get(k) or "").strip() for k in ("repo", "title", "brief"))
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
        sid = f"S{len(self._all) + 1}"
        try:
            name, cwd = await self._setup.workspace.repo(repo)
            branch = None
            if writes:
                holder = f"jarvis {sid} ({task_id}): {title}"
                cwd, branch = await self._setup.workspace.worktree(cwd, branch_name(kind, title), holder)
        except Refusal as e:
            return Refused(str(e))
        log = self._setup.log_dir / f"{sid}.jsonl" if self._setup.log_dir else None
        subagent = Subagent(
            sid, task_id, name, title, brief, model, effort, cwd, self._clock.now(), branch, log
        )
        self._all[sid] = subagent
        return subagent

    def start(self, subagent: Subagent) -> SubagentState:
        """Runs it now, or queues it behind the cap: which of the two it says."""
        ahead = [s for s in self._all.values() if s is not subagent and s.state not in FINISHED and s.started is None]
        running = [s for s in self._all.values() if s.state == "running"]
        self._runs.append(asyncio.create_task(self._run(subagent)))
        return "queued" if len(running) + len(ahead) >= self._setup.cap else "running"

    def get(self, subagent_id: object) -> Subagent | None:
        return self._all.get(str(subagent_id).strip().upper()) if subagent_id else None

    def live(self, task_id: str) -> list[Subagent]:
        return [s for s in self._all.values() if s.task_id == task_id and s.state not in FINISHED]

    def lines(self, task_id: str | None = None) -> list[str]:
        now = self._clock.now()
        return [s.line(now) for s in self._all.values() if task_id is None or s.task_id == task_id]

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

    async def close(self) -> None:
        """Stop every Subagent and wait for each to end; their worktrees and branches stay."""
        for subagent in self._all.values():
            subagent.stop.set()
        if self._runs:
            await asyncio.wait(self._runs)

    async def _run(self, subagent: Subagent) -> None:
        end, text = TurnEnd("interrupted"), ""
        held = False
        try:
            held = await self._take_slot(subagent)
            if held and not subagent.stop.is_set():
                subagent.state, subagent.started = "running", self._clock.now()
                end, text = await self._session(subagent)
        except Exception as e:
            end = TurnEnd("failed", error=f"{type(e).__name__}: {e}")
        finally:
            if held:
                self._slots.release()
        await self._finish(subagent, end, text)

    async def _take_slot(self, subagent: Subagent) -> bool:
        """Waits for a free slot, or for a stop while queued. True when the slot is held."""
        taking = asyncio.create_task(self._slots.acquire())
        stopping = asyncio.create_task(subagent.stop.wait())
        await asyncio.wait([taking, stopping], return_when=asyncio.FIRST_COMPLETED)
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
        session = await self._backend.open(spec)
        consuming = asyncio.create_task(self._consume(session, subagent.brief, log))
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
            stopping.cancel()
            timing.cancel()
            with contextlib.suppress(Exception):
                await session.close()

    async def _consume(self, session: AgentSession, brief: str, log: "_Log") -> tuple[TurnEnd, str]:
        """The turn's end, and the text after its last tool call: the Subagent's report."""
        end = TurnEnd("failed", error="the session ended without a result")
        block, final = "", ""
        async for event in session.send(brief):
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
        log.write("end", outcome=end.outcome, error=end.error, usage=asdict(end.usage))
        return end, final.strip()

    async def _watch_time(self, subagent: Subagent) -> None:
        while True:
            await self._clock.sleep(self._setup.time_limit)
            subagent.over_time += 1
            self._on_over_time(subagent)

    async def _finish(self, subagent: Subagent, end: TurnEnd, text: str) -> None:
        stopped = subagent.stop.is_set()
        if end.outcome == "completed":
            state: SubagentState = "done"
            report = text or "It ended without a report."
        elif end.outcome == "interrupted" and stopped:
            state = "cancelled"
            report = "Cancelled before it finished." if subagent.started else "Cancelled before it started."
        else:
            state = "failed"
            reason = end.error or "it stopped without the Hub stopping it"
            report = f"Failed: {reason}." + (f"\n\n{text}" if text else "")
        if subagent.writes:
            with contextlib.suppress(Exception):
                await self._setup.workspace.release(subagent.cwd)
            try:
                on_branch = await self._setup.workspace.describe(subagent.cwd)
            except Exception as e:
                on_branch = f"Could not read the branch: {e}"
            report += f"\n\nBranch {subagent.branch}, kept in {subagent.cwd}:\n{on_branch}"
        subagent.state, subagent.report, subagent.ended = state, report, self._clock.now()
        self._on_end(subagent)


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
        branch_line = "You only read: this is the User's own clone and no branch was made for you, so change nothing."
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
        system_prompt="\n".join(render(name, values) for name in ("subagent.md", "working-rules.md", "boundary.md")),
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
