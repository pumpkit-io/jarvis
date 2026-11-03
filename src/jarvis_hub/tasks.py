"""The Hub's record of every Task this launch: what the Talker asks about, answered without a model call."""

from dataclasses import dataclass
from typing import Literal

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

    @property
    def cancelled(self) -> bool:
        return self.state == "cancelled"

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

    def report(self, spoken: str, *, needs_user: bool) -> None:
        self.last_report = spoken
        self.reports += 1
        if self.waiting:
            self.state = "queued"
        else:
            self.state = "needs_user" if needs_user else "done"

    def fail(self, reason: str) -> None:
        self.state = "failed"
        self.last_report = f"Failed: {reason}."

    def cancel(self) -> bool:
        """False when the Task had already finished."""
        if self.state in FINISHED:
            return False
        self.state = "cancelled"
        return True


class Tasks:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def create(self, request: str, now: float) -> Task:
        task = Task(id=f"T{len(self._tasks) + 1}", request=request, created=now)
        self._tasks[task.id] = task
        return task

    def get(self, task_id: object) -> Task | None:
        return self._tasks.get(str(task_id).strip().upper()) if task_id else None

    def status(self, now: float) -> str:
        """One line per Task, newest first, for the Talker to read out of."""
        if not self._tasks:
            return "No Tasks this session."
        lines: list[str] = []
        for task in reversed(self._tasks.values()):
            line = f"{task.id} {task.state.replace('_', ' ')}, started {_ago(now - task.created)} ago: {task.request}"
            if task.last_report:
                line += f" | Last report: {task.last_report}"
            lines.append(line)
        return "\n".join(lines)


def no_task(task_id: object) -> str:
    return f"There is no Task {task_id}."


def _ago(seconds: float) -> str:
    if seconds < 90:
        return f"{max(0, round(seconds))} s"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} min"
    return f"{round(seconds / 3600, 1)} h"
