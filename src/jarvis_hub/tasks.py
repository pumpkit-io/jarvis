"""The Hub's record of every Task this launch, and the open ones from before a restart: what the Talker asks about,
answered without a model call."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from jarvis_hub.state import Fields, TimeShift, numbered, one_of

TaskState = Literal["queued", "working", "needs_user", "done", "failed", "cancelled"]
FINISHED: frozenset[TaskState] = frozenset({"done", "failed", "cancelled"})


@dataclass
class Task:
    """One Task's state. Every change goes through the methods below, in the order the Hub calls them."""

    id: str
    request: str
    created: float
    state: TaskState = "queued"
    last_report: str | None = None
    reports: int = 0
    # Messages for this Task still in the Orchestrator's queue.
    waiting: int = 0
    # What the User typed or said in the turns that started or continued this Task, never a model's rewording.
    words: list[str] = field(default_factory=lambda: [])
    revision: int = 0

    @property
    def cancelled(self) -> bool:
        return self.state == "cancelled"

    def revise(self, text: str) -> None:
        self.revision += 1
        self.request = text
        self.words.append(text)
        self.state = "working"

    def discard_message(self) -> None:
        self.waiting = max(0, self.waiting - 1)

    def queue_message(self) -> None:
        """A message for this Task joins the Orchestrator's queue; a Task being worked on stays working."""
        self.waiting += 1
        if self.state != "working":
            self.state = "queued"

    def take_message(self) -> bool:
        """The Orchestrator takes this Task's next message. False when the Task was cancelled meanwhile."""
        self.waiting -= 1
        if self.cancelled:
            return False
        self.state = "working"
        return True

    def report(self, spoken: str, *, needs_user: bool, live: bool) -> None:
        """`live`: Subagents, Watchers or Checks of this Task still run, so it keeps working after the report."""
        self.last_report = spoken
        self.reports += 1
        if self.waiting:
            self.state = "queued"
        elif needs_user:
            self.state = "needs_user"
        else:
            self.state = "working" if live else "done"

    def fail(self, reason: str, *, live: bool) -> None:
        self.last_report = f"Failed: {reason}."
        if self.waiting:
            self.state = "queued"
        elif not live:
            self.state = "failed"

    def settle(self) -> None:
        """Nothing more is coming for this Task, so one its last report left working is done."""
        if self.state == "working" and not self.waiting:
            self.state = "done"

    def cancel(self) -> bool:
        """False when the Task had already finished with nothing left in the queue for it."""
        if self.cancelled or (self.state in FINISHED and not self.waiting):
            return False
        self.state = "cancelled"
        return True

    @property
    def open(self) -> bool:
        """Not yet finished; a cancelled one with a message still queued counts."""
        return self.state not in FINISHED or (bool(self.waiting) and not self.cancelled)


_SAVED = Fields(("id", "request", "created", "state", "last_report", "reports", "words", "revision"), times=frozenset({"created"}))


class Tasks:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._ids = 0

    def create(self, request: str, now: float) -> Task:
        self._ids += 1
        task = Task(id=f"T{self._ids}", request=request, created=now)
        self._tasks[task.id] = task
        return task

    def dump(self, to_wall: TimeShift, *, include: set[str] | None = None) -> list[dict[str, Any]]:
        """The open Tasks, for the state file; finished ones end with the launch (story 24)."""
        return [_SAVED.dump(t, to_wall) for t in self._tasks.values() if t.open or t.id in (include or set())]

    @staticmethod
    def load(records: list[dict[str, Any]], from_wall: TimeShift) -> list[Task]:
        """Saved Tasks, checked whole and not yet restored. Their queued messages come back with the recovery
        message, so a queued one is working on it."""
        tasks: list[Task] = []
        for r in records:
            task = Task(**_SAVED.load({"revision": 0, **r}, from_wall))
            numbered(task.id, "T")
            if one_of(task.state, get_args(TaskState)) == "queued":
                task.state = "working"
            tasks.append(task)
        return tasks

    def restore(self, tasks: list[Task]) -> None:
        for task in tasks:
            self._tasks[task.id] = task
            self._ids = max(self._ids, numbered(task.id, "T"))

    def get(self, task_id: object) -> Task | None:
        return self._tasks.get(str(task_id).strip().upper()) if task_id else None

    def status(self, now: float, subagents: Callable[[str], list[str]] = lambda task_id: []) -> str:
        """One line per Task, newest first, then a line per Subagent of it, for the Talker to read out of."""
        if not self._tasks:
            return "No Tasks this session."
        return "\n".join(self._lines(reversed(self._tasks.values()), now, subagents))

    def open_status(self, now: float, subagents: Callable[[str], list[str]] = lambda task_id: []) -> str:
        """Like `status`, for the Tasks not yet finished (a cancelled one with a message still queued counts)."""
        open_tasks = [t for t in self._tasks.values() if t.open]
        return "\n".join(self._lines(reversed(open_tasks), now, subagents)) if open_tasks else "No open Tasks."

    @staticmethod
    def _lines(tasks: Iterable[Task], now: float, subagents: Callable[[str], list[str]]) -> list[str]:
        lines: list[str] = []
        for task in tasks:
            line = f"{task.id} {task.state.replace('_', ' ')}, started {ago(now - task.created)} ago: {task.request}"
            if task.last_report:
                line += f" | Last report: {task.last_report}"
            lines.append(line)
            lines += [f"  {sub}" for sub in subagents(task.id)]
        return lines


def no_task(task_id: object) -> str:
    return f"There is no Task {task_id}."


def ago(seconds: float) -> str:
    if seconds < 90:
        return f"{max(0, round(seconds))} s"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} min"
    return f"{round(seconds / 3600, 1)} h"
