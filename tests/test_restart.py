"""A Hub restart through the one seam: what was waited on comes back from the runtime folder's state file."""

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from jarvis_hub.hub import Hub
from tests.fakes import (
    AwaitInterrupt,
    FakeBackend,
    FakeClock,
    FakeWorkspace,
    Gate,
    RecordingMetrics,
    RecordingOutput,
    StubRunner,
    Text,
    Tool,
    Typist,
    lines,
    make_hub,
    until,
)

REVIEW = "Tell me when Ada reviews pumpkit#42"


def delegate(request: str = REVIEW) -> Tool:
    return Tool("delegate", {"request": request, "context": ""})


def check(after: int = 600, note: str = "Has Ada reviewed pumpkit#42?", repeats: int = 1) -> Tool:
    return Tool("schedule_check", {"after_seconds": after, "note": note, "max_repeats": repeats})


def report(task_id: str, spoken: str) -> Tool:
    return Tool("report", {"task_id": task_id, "spoken": spoken, "needs_user": False})


def received(backend: FakeBackend) -> list[str]:
    return [m for s in backend.all_sessions("orchestrator") for m in s.received]


def a_hub(clock: FakeClock, state: Path, output: RecordingOutput, **options: Any) -> tuple[Hub, FakeBackend]:
    scripts = options.pop("scripts", ())
    orchestrator = options.pop("orchestrator", ())
    backend = FakeBackend(clock, scripts, orchestrator=orchestrator, subagents=options.pop("subagents", None))
    return make_hub(backend, output, RecordingMetrics(), state_path=state, **options), backend


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / ".jarvis" / "state.json"


async def test_a_check_scheduled_before_a_restart_fires_after_it_on_time(clock: FakeClock, state: Path) -> None:
    first, _ = a_hub(
        clock,
        state,
        RecordingOutput(),
        scripts=[[Text("I'll keep an eye on it, sir."), delegate()]],
        orchestrator=[[check(), report("T1", "I'll look in ten minutes.")]],
    )
    await first.start()
    await first.run(lines(clock, REVIEW))
    await first.close()
    clock.advance(300)

    output = RecordingOutput()
    second, backend = a_hub(
        clock,
        state,
        output,
        scripts=[[Text("Ada has reviewed it, sir.")]],
        orchestrator=[[], [report("T1", "Ada approved pumpkit 42.")]],
    )
    typist = Typist(clock)
    await second.start()
    running = asyncio.create_task(second.run(typist.lines()))
    await until(lambda: len(received(backend)) == 1)
    clock.advance(299)
    await asyncio.sleep(0.01)
    assert len(received(backend)) == 1
    clock.advance(1)
    await until(lambda: output.replies[-1:] == ["Ada has reviewed it, sir."])
    typist.end()
    await running
    await second.close()

    assert received(backend)[1].startswith('<event source="timer" id="C1" task="T1" repeat="1" of="1">')


async def test_a_check_due_while_the_hub_was_down_fires_once_as_soon_as_it_is_back(
    clock: FakeClock, state: Path
) -> None:
    first, _ = a_hub(
        clock,
        state,
        RecordingOutput(),
        scripts=[[Text("I'll keep an eye on it, sir."), delegate()]],
        orchestrator=[[check(after=60, note="Look", repeats=3), report("T1", "Later.")]],
    )
    await first.start()
    await first.run(lines(clock, REVIEW))
    await first.close()
    clock.advance(3600)

    second, backend = a_hub(clock, state, RecordingOutput(), orchestrator=[[], []])
    await second.start()
    await until(lambda: len(received(backend)) == 2)
    await asyncio.sleep(0.01)
    await second.close()

    timers = [m for m in received(backend) if m.startswith('<event source="timer"')]
    assert timers == ['<event source="timer" id="C1" task="T1" repeat="1" of="3">Look</event>']


SPAWN = Tool(
    "spawn_subagent",
    {
        "repo": "pumpkit",
        "title": "Fix typo X",
        "brief": "Fix it.",
        "model": "sonnet",
        "effort": "medium",
        "writes": True,
        "branch_kind": "fix",
    },
)
WORKTREE = Path("/wt/pumpkit/fix-typo-x")


async def test_a_subagent_at_work_when_the_hub_crashed_comes_back_interrupted_with_its_lock_released(
    clock: FakeClock, state: Path, tmp_path: Path
) -> None:
    first, first_backend = a_hub(
        clock,
        state,
        RecordingOutput(),
        scripts=[[Text("On it."), delegate("Fix typo X in pumpkit")]],
        orchestrator=[[SPAWN]],
        subagents={"Fix it.": [AwaitInterrupt()]},
    )
    typist = Typist(clock)
    await first.start()
    running = asyncio.create_task(first.run(typist.lines()))
    typist.type("Fix typo X in pumpkit")
    await until(lambda: first_backend.all_sessions("subagent") and "\"S1\"" in state.read_text())
    crashed = tmp_path / "crashed" / "state.json"
    crashed.parent.mkdir()
    shutil.copy(state, crashed)

    workspace = FakeWorkspace()
    workspace.locks[WORKTREE] = "jarvis S1 (T1): Fix typo X"
    output = RecordingOutput()
    second, backend = a_hub(
        clock,
        crashed,
        output,
        scripts=[[Text("The typo fix was cut off, sir.")]],
        orchestrator=[[report("T1", "The typo fix was cut off by a restart; shall I start it again?")]],
        workspace=workspace,
    )
    await second.start()
    await until(lambda: output.replies[-1:] == ["The typo fix was cut off, sir."])
    await second.close()
    running.cancel()
    await first.close()

    [message] = received(backend)
    assert message.startswith('<event source="hub" reason="restart">')
    assert "T1 working" in message and "S1 interrupted" in message
    assert '<interrupted id="S1" task="T1" repo="pumpkit">Fix typo X (branch fix/fix-typo-x).' in message
    assert "Branch fix/fix-typo-x, kept in /wt/pumpkit/fix-typo-x" in message
    assert workspace.released == [WORKTREE] and not workspace.locks
    assert backend.all_sessions("subagent") == []


async def test_a_subagent_stopped_by_a_clean_close_is_reported_interrupted_on_the_next_launch(
    clock: FakeClock, state: Path
) -> None:
    workspace = FakeWorkspace()
    first, first_backend = a_hub(
        clock,
        state,
        RecordingOutput(),
        scripts=[[Text("On it."), delegate("Fix typo X in pumpkit")]],
        orchestrator=[[SPAWN]],
        subagents={"Fix it.": [AwaitInterrupt()]},
        workspace=workspace,
    )
    typist = Typist(clock)
    await first.start()
    running = asyncio.create_task(first.run(typist.lines()))
    typist.type("Fix typo X in pumpkit")
    await until(lambda: first_backend.all_sessions("subagent"))
    await first.close()
    running.cancel()
    assert workspace.released == [WORKTREE]
    assert received(first_backend) == [received(first_backend)[0]]

    second_workspace = FakeWorkspace()
    second, backend = a_hub(
        clock, state, RecordingOutput(), orchestrator=[[report("T1", "Cut off.")]], workspace=second_workspace
    )
    await second.start()
    await until(lambda: received(backend))
    await second.close()

    [message] = received(backend)
    assert '<interrupted id="S1" task="T1" repo="pumpkit">Fix typo X (branch fix/fix-typo-x). Interrupted:' in message
    assert second_workspace.released == []


async def test_a_watcher_restarts_its_gh_command_after_a_restart_and_its_end_wakes_the_orchestrator(
    clock: FakeClock, state: Path
) -> None:
    first, _ = a_hub(
        clock,
        state,
        RecordingOutput(),
        scripts=[[Text("I'll watch it, sir."), delegate("Tell me when CI run 7 on pumpkit ends")]],
        orchestrator=[[Tool("watch", {"kind": "ci_run", "target": "pumpkit#7"}), report("T1", "Watching it.")]],
    )
    typist = Typist(clock)
    await first.start()
    running = asyncio.create_task(first.run(typist.lines()))
    typist.type("Tell me when CI run 7 on pumpkit ends")
    await until(lambda: '"W1"' in (state.read_text() if state.exists() else ""))
    await first.close()
    running.cancel()

    runner = StubRunner()
    second, backend = a_hub(
        clock, state, RecordingOutput(), orchestrator=[[], [report("T1", "CI is green.")]], runner=runner
    )
    await second.start()
    running = asyncio.create_task(second.run(lines(clock)))
    await until(lambda: runner.started)
    runner.finish(0, 0, "Run 7 completed: success")
    await running
    await second.close()

    assert runner.viewed == [("gh", "run", "view", "7", "-R", "pumpkit-io/pumpkit", "--json", "status")]
    watcher = [m for m in received(backend) if m.startswith('<event source="watcher"')]
    assert watcher == [
        '<event source="watcher" id="W1" task="T1" kind="ci_run" target="pumpkit#7" status="succeeded">'
        "Run 7 completed: success</event>"
    ]


async def test_with_nothing_pending_a_restart_makes_no_model_call_beyond_the_warm_up(
    clock: FakeClock, state: Path
) -> None:
    first, _ = a_hub(
        clock,
        state,
        RecordingOutput(),
        scripts=[[Text("Let me look."), delegate("How many PRs are open?")]],
        orchestrator=[[report("T1", "Three.")]],
    )
    await first.start()
    await first.run(lines(clock, "How many PRs are open?"))
    await first.close()

    second, backend = a_hub(clock, state, RecordingOutput())
    await second.start()
    await second.run(lines(clock))
    await second.close()

    assert backend.all_sessions("orchestrator") == []


async def test_an_open_task_nothing_will_wake_fails_when_the_recovery_turn_leaves_it(
    clock: FakeClock, state: Path
) -> None:
    gate = asyncio.Event()
    first, first_backend = a_hub(
        clock,
        state,
        RecordingOutput(),
        scripts=[[Text("Let me look."), delegate("How many PRs are open?")]],
        orchestrator=[[Gate(gate)]],
    )
    typist = Typist(clock)
    await first.start()
    running = asyncio.create_task(first.run(typist.lines()))
    typist.type("How many PRs are open?")
    await until(lambda: first_backend.all_sessions("orchestrator"))
    await first.close()
    running.cancel()

    output = RecordingOutput()
    second, backend = a_hub(clock, state, output, scripts=[[Text("I lost that one, sir.")]], orchestrator=[[]])
    await second.start()
    await second.run(lines(clock))
    await second.close()

    talker = [m for s in backend.all_sessions("talker") for m in s.received]
    assert any("T1 failed: the Hub restarted and the Orchestrator did not take it up again." in m for m in talker)


@pytest.mark.parametrize(
    ("content", "problem"),
    [("{not json", "is not valid JSON"), ('{"version": 99, "tasks": []}', "has version 99, not 1")],
)
async def test_an_unreadable_state_file_is_moved_aside_with_a_notice_and_the_hub_starts_fresh(
    clock: FakeClock, state: Path, content: str, problem: str
) -> None:
    state.parent.mkdir(parents=True)
    state.write_text(content)
    output = RecordingOutput()
    hub, backend = a_hub(clock, state, output)

    await hub.start()
    await hub.run(lines(clock, "Hello"))
    await hub.close()

    aside = state.with_name("state.unreadable.json")
    assert output.notices == [f"The state file {state} {problem}, so nothing was restored; it was moved to {aside}."]
    assert aside.read_text() == content
    assert backend.all_sessions("orchestrator") == []
    assert json.loads(state.read_text())["version"] == 1
